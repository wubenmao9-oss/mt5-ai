import ast, re

# Check server.py
with open("src/web/server.py", "r", encoding="utf-8") as f:
    c = f.read()
try:
    ast.parse(c)
    print("server.py: syntax OK")
except SyntaxError as e:
    print(f"server.py: ERROR {e.lineno}: {e.msg}")

# Check config endpoint exists  
if 'to_dict()' in c and 'StrategyConfig' in c:
    print("Config endpoint: uses StrategyConfig")

# Check index.html
with open("src/web/static/index.html", "r", encoding="utf-8") as f:
    c = f.read()

# Check for direction label
m = re.search(r"const label = isBuy.*? : '.*?'", c)
if m:
    print(f"Direction label: {m.group()}")
else:
    print("Direction label: NOT FOUND (need fix)")

# Check Chinese chars
cn = len(re.findall(r"[\u4e00-\u9fff]+", c))
print(f"Chinese chars in HTML: {cn}")

# Check remaining ???
print(f"??? remaining: {c.count('???')}")
