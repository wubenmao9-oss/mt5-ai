#property strict
#define CMD "cmd.txt"
#define RSP "rsp.txt"

int OnInit() {
   EventSetTimer(2);
   return(INIT_SUCCEEDED);
}

void OnTimer() {
   int h = FileOpen(CMD, FILE_READ|FILE_TXT, 0, CP_ACP);
   if (h == INVALID_HANDLE) return;
   
   string a = "";
   while (!FileIsEnding(h)) {
      string l = FileReadString(h);
      int s = StringFind(l, ":");
      if (s < 0) continue;
      if (StringSubstr(l, 0, s) == "ACTION") a = StringSubstr(l, s + 1);
   }
   FileClose(h);
   FileDelete(CMD);
   
   string r = "";
   if (a == "ping") r = "STATUS:ok\nPONG:1";
   else r = "STATUS:error\nERR:?";
   
   int rh = FileOpen(RSP, FILE_WRITE|FILE_TXT, 0, CP_ACP);
   if (rh != INVALID_HANDLE) {
      FileWriteString(rh, r);
      FileClose(rh);
   }
}
