# Update server.py - add return _state to init()
with open("src/web/server.py","r",encoding="utf-8") as f:
    c = f.read()
c = c.replace('    logger.info("Web API initialized")', '    logger.info("Web API initialized")\n    return _state')
with open("src/web/server.py","w",encoding="utf-8") as f:
    f.write(c)

# Update main.py - capture app_state and update in loop
with open("main.py","r",encoding="utf-8") as f:
    c = f.read()
c = c.replace("init_web(manager)", "app_state=init_web(manager)")
c = c.replace('logger.info("[Status] %s  pos=%s  streak=%s  trades=%d  PnL=%.2f  silence=%s",\n                         st["state"], "Open" if st["position"] else "None", st["streak"],\n                         s["total_trades"], s["total_profit"], st.get("silence_reason","?"))',
              'logger.info("[Status] %s  pos=%s  streak=%s  trades=%d  PnL=%.2f  silence=%s",\n                         st["state"], "Open" if st["position"] else "None", st["streak"],\n                         s["total_trades"], s["total_profit"], st.get("silence_reason","?"))\n            app_state.daily_pnl=s["total_profit"]\n            app_state.daily_trades=s["total_trades"]\n            app_state.engine_state=st.get("state","RUNNING")\n            app_state.consecutive_losses=st.get("cl",0)')
with open("main.py","w",encoding="utf-8") as f:
    f.write(c)

import ast
ast.parse(open("src/web/server.py","r",encoding="utf-8").read())
print("server.py: Syntax OK")
ast.parse(open("main.py","r",encoding="utf-8").read())
print("main.py: Syntax OK")
print("Both files updated with state sync")
