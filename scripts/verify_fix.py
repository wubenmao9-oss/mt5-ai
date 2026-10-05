import ast
c=open("src/strategy/engine_v4.py","r",encoding="utf-8").read()
ast.parse(c)
print("Syntax OK")
for p in ["(tp - entry) * 1.0","hold_m=","Idle","pnl=0.0"]:
    ok = "OK" if p in c else "MISSING"
    print(f"  {p}: {ok}")
