//+------------------------------------------------------------------+
//|                                               PythonBridgeEA.mq4 |
//|  Python MT4 文件桥 — 读取 cmd.txt 执行交易，写入 rsp.txt 返回     |
//+------------------------------------------------------------------+
#property strict

#define CMD_FILE   "cmd.txt"
#define RSP_FILE   "rsp.txt"

int OnInit() {
   Print("PythonBridgeEA 启动 - 等待命令...");
   return(INIT_SUCCEEDED);
}

string JoinLine(string key, string val) {
   return(key + ":" + val);
}

void OnTick() {
   if (!FileIsExist(CMD_FILE)) return;

   int h = FileOpen(CMD_FILE, FILE_READ|FILE_TXT, 0, CP_ACP);
   if (h == INVALID_HANDLE) return;

   string action = "";
   string symbol = Symbol();
   double volume = 0.01;
   int sl_points = 0, tp_points = 0;
   int magic = 0, slippage = 10;

   while (!FileIsEnding(h)) {
      string line = FileReadString(h);
      int sep = StringFind(line, ":");
 if (sep < 0) continue;
      string key = StringSubstr(line, 0, sep);
      string val = StringSubstr(line, sep + 1);
           if (key == "ACTION")   action     = val;
      else if (key == "SYMBOL")   symbol     = val;
      else if (key == "VOLUME")   volume     = StringToDouble(val);
      else if (key == "SL_POINTS") sl_points = StringToInteger(val);
      else if (key == "TP_POINTS") tp_points = StringToInteger(val);
      else if (key == "MAGIC")    magic      = StringToInteger(val);
      else if (key == "SLIPPAGE") slippage   = StringToInteger(val);
   }
   FileClose(h);
   FileDelete(CMD_FILE);

   string result = "";
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   if (digits == 0) digits = 5;

   if (action == "ping") {
      result = "STATUS:ok\nPONG:1";
   }
   else if (action == "quote") {
      result = JoinLine("STATUS","ok") + "\n" +
               JoinLine("SYMBOL", Symbol()) + "\n" +
               JoinLine("BID", DoubleToStr(Bid, digits)) + "\n" +
               JoinLine("ASK", DoubleToStr(Ask, digits)) + "\n" +
               JoinLine("DIGITS", IntegerToString(digits));
   }
   else if (action == "account") {
      result = JoinLine("STATUS","ok") + "\n" +
               JoinLine("BALANCE", DoubleToStr(AccountBalance(), 2)) + "\n" +
               JoinLine("EQUITY", DoubleToStr(AccountEquity(), 2)) + "\n" +
               JoinLine("MARGIN", DoubleToStr(AccountMargin(), 2)) + "\n" +
               JoinLine("FREEMARGIN", DoubleToStr(AccountFreeMargin(), 2)) + "\n" +
               JoinLine("PROFIT", DoubleToStr(AccountProfit(), 2)) + "\n" +
               JoinLine("LEVERAGE", IntegerToString(AccountLeverage())) + "\n" +
               JoinLine("SERVER", AccountServer()) + "\n" +
               JoinLine("NAME", AccountName()) + "\n" +
               JoinLine("NUMBER", IntegerToString(AccountNumber()));
   }
   else if (action == "buy" || action == "sell") {
      int cmd = (action == "buy") ? OP_BUY : OP_SELL;
      double price = (cmd == OP_BUY) ? Ask : Bid;
      double sl = 0, tp = 0;
      if (sl_points > 0) sl = (cmd == OP_BUY) ? price - sl_points * Point : price + sl_points * Point;
      if (tp_points > 0) tp = (cmd == OP_BUY) ? price + tp_points * Point : price - tp_points * Point;

      int ticket = OrderSend(symbol, cmd, volume, price, slippage, sl, tp,
                            "PythonBridge", magic, 0, clrNONE);
      if (ticket > 0) {
         result = JoinLine("STATUS","ok") + "\n" +
                  JoinLine("ORDER", IntegerToString(ticket)) + "\n" +
                  JoinLine("PRICE", DoubleToStr(price, digits)) + "\n" +
                  JoinLine("SYMBOL", symbol) + "\n" +
                  JoinLine("VOLUME", DoubleToStr(volume, 2));
      } else {
         result = JoinLine("STATUS","error") + "\n" +
                  JoinLine("ERRCODE", IntegerToString(GetLastError()));
      }
   }
   else if (action == "close_all") {
      int c = 0;
      for (int i = OrdersTotal() - 1; i >= 0; i--) {
         if (OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
            if (symbol != "" && OrderSymbol() != symbol) continue;
            if (OrderClose(OrderTicket(), OrderLots(), OrderClosePrice(), slippage, clrNONE)) c++;
         }
      }
      result = JoinLine("STATUS","ok") + "\n" +
               JoinLine("CLOSED", IntegerToString(c));
   }
   else if (action == "orders") {
      string list = "";
      for (int i = 0; i < OrdersTotal(); i++) {
         if (OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
            int od = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
            if (od == 0) od = digits;

            list += "ORDER:" + IntegerToString(OrderTicket()) +
                    "|SYMBOL:" + OrderSymbol() +
                    "|TYPE:" + IntegerToString(OrderType()) +
                    "|VOLUME:" + DoubleToStr(OrderLots(), 2) +
                    "|OPENPRICE:" + DoubleToStr(OrderOpenPrice(), od) +
                    "|PROFIT:" + DoubleToStr(OrderProfit(), 2) +
                    "|SWAP:" + DoubleToStr(OrderSwap(), 2) + "\n";
         }
      }
      result = JoinLine("STATUS","ok") + "\n" +
               JoinLine("TOTAL", IntegerToString(OrdersTotal())) + "\n" + list;
   }
   else {
      result = JoinLine("STATUS","error") + "\n" +
               JoinLine("ERRCODE", "unknown_action") + "\n" +
               JoinLine("ACTION", action);
   }

   int rh = FileOpen(RSP_FILE, FILE_WRITE|FILE_TXT, 0, CP_ACP);
   if (rh != INVALID_HANDLE) {
      FileWriteString(rh, result);
      FileClose(rh);
   }
}
//+------------------------------------------------------------------+
