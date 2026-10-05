"""Strategy encryption / decryption / dynamic loading.

Encrypts compiled .pyc bytecode with AES-256-CBC so users cannot read source.
At runtime, decrypts and loads the module via importlib.
"""

import hashlib, os, struct, importlib, types, sys
from pathlib import Path

# Obfuscated key derivation — derived from app identity, not stored plainly
_SEED = b"BenmaoMT5\x00StrategyCloud\x00v1"
_KEY = hashlib.sha256(_SEED + hashlib.sha256(_SEED).digest()).digest()  # 32 bytes

def _xor_mask(data: bytes, key: bytes) -> bytes:
    """Simple repeating-XOR mask for additional obfuscation layer."""
    out = bytearray(len(data))
    klen = len(key)
    for i in range(len(data)):
        out[i] = data[i] ^ key[i % klen]
    return bytes(out)

def encrypt_pyc(pyc_bytes: bytes) -> bytes:
    """Encrypt .pyc bytecode → .enc format.

    Format: [4B magic][16B iv][N encrypted bytes][4B checksum]
    Encryption: AES-256-CBC(pyc_bytes) then XOR mask.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding
    except ImportError:
        # Fallback: simple XOR-only encryption if cryptography not installed
        iv = os.urandom(16)
        enc = _xor_mask(pyc_bytes, _KEY[:16] + iv)
        chk = struct.pack("<I", hashlib.sha256(pyc_bytes).digest()[0] & 0xFFFFFFFF)
        return b"BMC1" + iv + enc + chk

    iv = os.urandom(16)
    # PKCS7 pad
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(pyc_bytes) + padder.finalize()
    # AES-CBC encrypt
    cipher = Cipher(algorithms.AES(_KEY), modes.CBC(iv))
    enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
    # XOR mask layer
    enc = _xor_mask(enc, iv + _KEY[:16])
    chk = struct.pack("<I", hashlib.sha256(pyc_bytes).digest()[0] & 0xFFFFFFFF)
    return b"BMC1" + iv + enc + chk

def decrypt_pyc(enc_bytes: bytes) -> bytes:
    """Decrypt .enc format → original .pyc bytecode."""
    if len(enc_bytes) < 24 or enc_bytes[:4] != b"BMC1":
        raise ValueError("Invalid strategy file format")
    iv = enc_bytes[4:20]
    chk = struct.unpack("<I", enc_bytes[-4:])[0]
    payload = enc_bytes[20:-4]

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding
        # Reverse XOR mask
        payload = _xor_mask(payload, iv + _KEY[:16])
        # AES-CBC decrypt
        cipher = Cipher(algorithms.AES(_KEY), modes.CBC(iv))
        decrypted = cipher.decryptor().update(payload) + cipher.decryptor().finalize()
        # Remove PKCS7 padding
        unpadder = sym_padding.PKCS7(128).unpadder()
        pyc_bytes = unpadder.update(decrypted) + unpadder.finalize()
    except ImportError:
        # Fallback: XOR-only
        pyc_bytes = _xor_mask(payload, _KEY[:16] + iv)

    # Verify checksum
    actual = hashlib.sha256(pyc_bytes).digest()[0] & 0xFFFFFFFF
    if actual != chk:
        raise ValueError("Strategy file corrupted or tampered")
    return pyc_bytes

def compile_to_pyc(source_code: str, module_name: str) -> bytes:
    """Compile Python source code to .pyc bytecode in memory."""
    code = compile(source_code, f"<{module_name}>", "exec")
    # Use importlib's internal machinery to produce .pyc bytes
    import importlib.util
    # Manual .pyc construction for Python 3.8+
    import time as _time
    t = int(_time.time())
    # The simplest way: write temp .py, compile, read .pyc, cleanup
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="bmc_")
    try:
        src_path = Path(tmp_dir) / f"{module_name}.py"
        src_path.write_text(source_code, encoding="utf-8")
        py_compile = __import__("py_compile")
        pyc_path = Path(tmp_dir) / "__pycache__" / f"{module_name}.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
        py_compile.compile(str(src_path), str(pyc_path), doraise=True)
        return pyc_path.read_bytes()
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

def decrypt_to_module(enc_bytes: bytes, module_name: str) -> types.ModuleType:
    """Decrypt .enc file and load as a Python module via importlib.

    Returns the loaded module object. The strategy class can be found
    by iterating module attributes for BaseStrategy subclasses.
    """
    pyc_bytes = decrypt_pyc(enc_bytes)
    # Create a blank module
    mod = types.ModuleType(module_name)
    mod.__file__ = f"<cloud:{module_name}>"
    mod.__package__ = "src.strategy"
    # Ensure src.strategy is in sys.modules for absolute imports
    if "src.strategy" not in sys.modules:
        sys.modules["src.strategy"] = types.ModuleType("src.strategy")
    if "src.strategy.base" not in sys.modules:
        # Import BaseStrategy properly
        import importlib
        try:
            sys.modules["src.strategy.base"] = importlib.import_module("src.strategy.base")
        except Exception:
            pass
    # Execute the code object in the module's namespace
    # For .pyc we need to skip the header and unmarshal the code object
    import marshal
    # Python 3.8+ .pyc format: 16 bytes header, then marshalled code
    header_size = 16
    if len(pyc_bytes) > header_size:
        code_obj = marshal.loads(pyc_bytes[header_size:])
    else:
        raise ValueError("Invalid .pyc data")
    exec(code_obj, mod.__dict__)
    # Register in sys.modules so imports work
    sys.modules[module_name] = mod
    return mod

def extract_strategy_class(module: types.ModuleType):
    """Find the BaseStrategy subclass in a loaded module.

    Returns (class_name, class_object) or raises if not found.
    """
    from .base import BaseStrategy
    for name in dir(module):
        obj = getattr(module, name)
        if (isinstance(obj, type)
            and issubclass(obj, BaseStrategy)
            and obj is not BaseStrategy):
            return name, obj
    raise ValueError(f"No BaseStrategy subclass found in module {module.__name__}")
