with open("src/strategy/engine_v4.py","r",encoding="utf-8") as f:
    c = f.read()

old_trail = """    async def _trailing_stop_loop(self):
        \"\"\"Check SL/TP on each M5 close\"\"\"
        while self._running and self._position:
            try:
                pos=self._position
                if pos is None: break
                q=await self._exe.get_quote(self._cfg.symbol)
                if q is None: await asyncio.sleep(2); continue
                bid=q["bid"]; ask=q["ask"]
                sl=pos["sl"]; tp=pos["tp"]; entry=pos["entry"]
                if pos["dir"]=="long":
                    if bid<=sl:
                        await self._close_position("SL")
                    elif bid>=tp:
                        await self._close_position("TP")
                else:
                    if ask>=sl:
                        await self._close_position("SL")
                    elif ask<=tp:
                        await self._close_position("TP")
                await asyncio.sleep(2)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.warning("V4 trailing: %s", e)
                await asyncio.sleep(5)"""

new_trail = """    async def _trailing_stop_loop(self):
        \"\"\"Multi-phase trailing: BE($3) -> Lock($6) -> TP($10)\"\"\"
        while self._running and self._position:
            try:
                pos=self._position
                if pos is None: break
                q=await self._exe.get_quote(self._cfg.symbol)
                if q is None: await asyncio.sleep(2); continue
                bid=q["bid"]; ask=q["ask"]
                entry=pos["entry"]; sl=pos["sl"]; tp=pos["tp"]
                be=pos.get("be",False)

                if pos["dir"]=="long":
                    pnl=(bid-entry)*1.0
                    if bid<=sl:
                        await self._close_position("BE_SL" if be else "SL"); break
                    if bid>=tp:
                        await self._close_position("TP"); break
                    if pnl>=3.0 and not be:
                        ns=round(entry+0.5,2); pos["sl"]=ns; pos["be"]=True
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 BE: PnL=%.2f SL=%.2f",pnl,ns)
                    elif be and pnl>=6.0 and sl<entry+4.0:
                        ns=round(entry+4.0,2); pos["sl"]=ns
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 LOCK4: PnL=%.2f SL=%.2f",pnl,ns)
                else:
                    pnl=(entry-ask)*1.0
                    if ask>=sl:
                        await self._close_position("BE_SL" if be else "SL"); break
                    if ask<=tp:
                        await self._close_position("TP"); break
                    if pnl>=3.0 and not be:
                        ns=round(entry-0.5,2); pos["sl"]=ns; pos["be"]=True
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 BE: PnL=%.2f SL=%.2f",pnl,ns)
                    elif be and pnl>=6.0 and sl>entry-4.0:
                        ns=round(entry-4.0,2); pos["sl"]=ns
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 LOCK4: PnL=%.2f SL=%.2f",pnl,ns)
                await asyncio.sleep(2)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.warning("V4 trailing: %s", e)
                await asyncio.sleep(5)"""

c = c.replace(old_trail, new_trail)

# Also update the close reason handling
old_close = """        # Email notification
        if self._emailer:
            self._emailer.send_close(datetime.now().strftime("%H:%M:%S"),dir_label,reason,round(pnl,2),"")
        self._position=None
        logger.info("V4 closed %s reason=%s pnl=%.2f", dir_label, reason, pnl)"""

new_close = """        # Email notification
        reason_label = reason
        if reason == "BE_SL": reason_label = "BE"  # Breakeven stop-out is still a win
        if self._emailer:
            self._emailer.send_close(datetime.now().strftime("%H:%M:%S"),dir_label,reason_label,round(pnl,2),"")
        self._position=None
        logger.info("V4 closed %s reason=%s pnl=%.2f hold=%.0fmin", dir_label, reason, pnl,
                     (datetime.now(timezone.utc).timestamp()-pos.get("et",0))/60)"""

c = c.replace(old_close, new_close)

with open("src/strategy/engine_v4.py","w",encoding="utf-8") as f:
    f.write(c)

import ast
ast.parse(open("src/strategy/engine_v4.py","r",encoding="utf-8").read())
print("V4 updated: Multi-phase trailing stop added")
print("  Phase 1: TP=$10 / SL=original")
print("  Phase 2: At $3 profit -> move SL to BE+$0.50")
print("  Phase 3: At $6 profit -> lock $4 profit (SL=$4)")
print("  Final: TP=$10 exits full profit")
