# Register V3
with open("src/strategy/__init__.py","r",encoding="utf-8") as f:
    c = f.read()
c = c.replace("from .engine_v4 import StrategyEngineV4", "from .engine_v3 import StrategyEngineV3\nfrom .engine_v4 import StrategyEngineV4")
with open("src/strategy/__init__.py","w",encoding="utf-8") as f:
    f.write(c)

with open("main.py","r",encoding="utf-8") as f:
    c = f.read()
c = c.replace(", StrategyEngineV4", ", StrategyEngineV3, StrategyEngineV4")
c = c.replace('manager.register("V2", StrategyEngineV2)\n    manager.register', 'manager.register("V2", StrategyEngineV2)\n    manager.register("V3", StrategyEngineV3)\n    manager.register')
with open("main.py","w",encoding="utf-8") as f:
    f.write(c)

# Switch .env to V3
with open(".env","r") as f:
    c = f.read()
c = c.replace("ACTIVE_STRATEGY=V2", "ACTIVE_STRATEGY=V3")
with open(".env","w") as f:
    f.write(c)

print("V3 registered and activated")
print()

# Verify
with open("main.py") as f:
    for i,line in enumerate(f.readlines()):
        if "register" in line:
            print(f"  main.py L{i+1}: {line.rstrip()}")
with open(".env") as f:
    for line in f:
        if "ACTIVE" in line:
            print(f"  .env: {line.rstrip()}")
