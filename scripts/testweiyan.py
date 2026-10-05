import hashlib
import time
import json
import random
import requests

# ========== 配置区（请根据实际替换） ==========
HOST = "https://wy.llua.cn/v2/"
APPKEY = "你的APPKEY"                 # 请替换为实际值
ID = "你的ID"                         # 请替换为实际值
APITOKEN = "你的APITOKEN"             # 请替换为实际值
KAMI = "你的卡密"                     # 卡密

SUCCESS_CODE = 0                  # 登录成功状态码，请根据实际API定义修改
CHECK_RULE = "expected_value"  # 自定义校验规则，请根据实际修改
TIME_TOLERANCE = 30         # 时间戳容差（秒）

# ========== 工具函数 ==========
def get_device_code():
    """获取设备码（示例：使用MAC地址，可自行修改）"""
    import uuid
    return hex(uuid.getnode())[2:]

def get_timestamp():
    """获取当前Unix时间戳（秒）"""
    return int(time.time())

def get_random_value():
    """生成随机数（示例：6位随机整数）"""
    return random.randint(100000, 999999)

def md5_sign(data):
    """计算MD5签名（返回十六进制小写字符串）"""
    return hashlib.md5(data.encode('utf-8')).hexdigest()

# ========== 自定义加密/解密（占位，请按需实现） ==========
def custom_encrypt(plaintext: str) -> str:
    """
    自定义算法加密（占位函数）
    请替换为实际的加密逻辑，返回加密后的字符串（通常为Base64或Hex）
    """
    # 示例：不做任何加密（直接返回原文本），实际应替换
    return plaintext

def custom_decrypt(ciphertext: str) -> str:
    """
    自定义算法解密（占位函数）
    请替换为与加密对应的解密逻辑，返回明文字符串
    """
    # 示例：不做任何解密（直接返回原文本），实际应替换
    return ciphertext

# ========== 主流程 ==========
def main():
    # 1. 计算签名
    kami = KAMI
    markcode = get_device_code()
    t = get_timestamp()
    sign_raw = f"kami={kami}&markcode={markcode}&t={t}&{APPKEY}"
    sign = md5_sign(sign_raw)
    print(f"[DEBUG] sign_raw: {sign_raw}")
    print(f"[DEBUG] sign: {sign}")

    # 2. 拼接并加密参数
    random_val = get_random_value()
    plain_params = f"id={ID}&kami={kami}&markcode={markcode}&t={t}&sign={sign}&value={random_val}"
    print(f"[DEBUG] plain_params: {plain_params}")
    post_data = custom_encrypt(plain_params)
    print(f"[DEBUG] encrypted post_data: {post_data}")

    # 3. 发送POST请求
    url = HOST + APITOKEN
    # 根据实际接口要求，可能为json或form-data，这里假设以raw字符串发送
    headers = {"Content-Type": "application/x-www-form-urlencoded"}  # 或 "application/json"
    try:
        response = requests.post(url, data=post_data, headers=headers, timeout=10)
        response.raise_for_status()
        response_data = response.text
        print(f"[DEBUG] response raw: {response_data}")
    except Exception as e:
        print(f"请求失败: {e}")
        return

    # 4. 解密结果
    result = custom_decrypt(response_data)
    print(f"[DEBUG] decrypted result: {result}")

    # 5. 解析JSON并处理
    try:
        json_data = json.loads(result)
    except json.JSONDecodeError:
        print("解密后的结果不是合法JSON，请检查解密算法")
        return

    if json_data.get("code") == SUCCESS_CODE:
        # 时间戳校验（防重放）
        server_time = json_data.get("time")
        if server_time is not None:
            diff = server_time - t
            if diff > TIME_TOLERANCE or diff < -TIME_TOLERANCE:
                print("数据过期")
                return

        # check校验（可选）
        check = json_data.get("check")
        if check is not None and check != CHECK_RULE:
            print("校验失败")
            return

        # 其他校验逻辑（若有）...

        print("欢迎使用")
    else:
        msg = json_data.get("msg", "未知错误")
        print(f"登录失败，原因: {msg}")

if __name__ == "__main__":
    main()