#property strict
#property version   "3.00"
#property description "Mr Gold Algo Free Sync V3 - automatic V1.58 Unique ID tracking for the free Supabase/Render portal."

input string InpWebsiteUrl = "https://YOUR-SITE.onrender.com";
input string InpApiKey = "PASTE_API_KEY_HERE";
input int    InpSyncEverySeconds = 300;          // 5 min: low DB use and keeps free web service active while MT5 runs
input int    InpHttpTimeoutMs = 70000;           // allows a free host cold-start
input int    InpInitialHistoryDays = 730;        // first sync after EA starts
input int    InpRecentHistoryDays = 14;          // later syncs only resend recent deals; server deduplicates tickets
input bool   InpAutoDetectMrGoldV158 = true;
input ulong  InpMrGoldBaseMagic = 1517;
input bool   InpAutoStrategyFromComment = true;
input string InpMagicMappings = "";             // optional: EffectiveMagic=Strategy;...
input string InpUniqueIdMappings = "";          // optional fallback: EffectiveMagic=UniqueID;...
input string InpDefaultStrategy = "MR Gold V1.58";
input string InpDefaultUniqueId = "Unassigned";
input bool   InpSendSnapshots = true;
input bool   InpSendClosedTrades = true;

bool g_first_history_sync = true;

string JsonEscape(string s){
   StringReplace(s,"\\","\\\\"); StringReplace(s,"\"","\\\"");
   StringReplace(s,"\r","\\r"); StringReplace(s,"\n","\\n"); return s;
}
string Iso(datetime t){
   MqlDateTime z; TimeToStruct(t,z);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",z.year,z.mon,z.day,z.hour,z.min,z.sec);
}
string CleanBaseUrl(string s){
   StringTrimLeft(s); StringTrimRight(s);
   while(StringLen(s)>0 && StringSubstr(s,StringLen(s)-1,1)=="/") s=StringSubstr(s,0,StringLen(s)-1);
   return s;
}
string ValueForMagic(string mappings,long magic){
   string parts[]; int n=StringSplit(mappings,';',parts);
   for(int i=0;i<n;i++){
      string kv[];
      if(StringSplit(parts[i],'=',kv)==2){
         if((long)StringToInteger(kv[0])==magic){
            string v=kv[1]; StringTrimLeft(v); StringTrimRight(v); return v;
         }
      }
   }
   return "";
}
string StrategyForMagic(long magic){
   string v=ValueForMagic(InpMagicMappings,magic);
   if(v!="") return v;
   return InpDefaultStrategy+" (Magic "+IntegerToString((int)magic)+")";
}

// Native MR Gold V1.58 comments:
// PD|4450.00|B|1|SLP=1000.0
// AS|4420.00|S|15|SLP=1000.0
// 4th field = InpInstanceID / Unique ID.
string ExtractV158UniqueIdFromComment(string comment){
   if(comment=="") return "";
   string parts[]; int n=StringSplit(comment,'|',parts);
   if(n<4) return "";
   if(parts[0]!="PD" && parts[0]!="AS") return "";
   string id=parts[3]; StringTrimLeft(id); StringTrimRight(id);
   long v=(long)StringToInteger(id);
   if(v<1 || v>999) return "";
   return IntegerToString((int)v);
}
string StrategyFromV158Comment(string comment){
   if(!InpAutoStrategyFromComment || comment=="") return "";
   string parts[]; int n=StringSplit(comment,'|',parts);
   if(n<1) return "";
   if(parts[0]=="PD") return "Previous Day";
   if(parts[0]=="AS") return "Asia Session";
   return "";
}
string UniqueIdFromV158Magic(long magic){
   if(!InpAutoDetectMrGoldV158 || InpMrGoldBaseMagic==0 || magic<=0) return "";
   long base=(long)InpMrGoldBaseMagic;
   long quotient=magic/1000;
   long instance=magic%1000;
   if(quotient!=base || instance<1 || instance>999) return "";
   return IntegerToString((int)instance);
}
string ExtractUniqueIdFromComment(string comment){
   if(comment=="") return "";
   string upper=comment; StringToUpper(upper);
   string keys[5]={"UID=","UNIQUEID=","UNIQUE_ID=","UNIQUE ID=","UID:"};
   for(int k=0;k<5;k++){
      int p=StringFind(upper,keys[k]); if(p<0) continue;
      int start=p+StringLen(keys[k]), total=StringLen(comment), end=start;
      while(end<total && end-start<80){
         string ch=StringSubstr(comment,end,1);
         if(ch=="|" || ch==";" || ch=="," || ch==" " || ch=="\t" || ch=="]" || ch==")" || ch=="/") break;
         end++;
      }
      string value=StringSubstr(comment,start,end-start); StringTrimLeft(value); StringTrimRight(value);
      if(value!="") return value;
   }
   return "";
}
string UniqueIdForTrade(long magic,string exit_comment,string entry_comment){
   string uid=ExtractUniqueIdFromComment(exit_comment);
   if(uid=="") uid=ExtractUniqueIdFromComment(entry_comment);
   if(uid=="" && InpAutoDetectMrGoldV158) uid=ExtractV158UniqueIdFromComment(entry_comment);
   if(uid=="" && InpAutoDetectMrGoldV158) uid=ExtractV158UniqueIdFromComment(exit_comment);
   if(uid=="") uid=UniqueIdFromV158Magic(magic);
   if(uid=="") uid=ValueForMagic(InpUniqueIdMappings,magic);
   if(uid=="") uid=InpDefaultUniqueId;
   return uid;
}
string StrategyForTrade(long magic,string exit_comment,string entry_comment){
   string mapped=ValueForMagic(InpMagicMappings,magic);
   if(mapped!="") return mapped;
   string v=StrategyFromV158Comment(entry_comment);
   if(v=="") v=StrategyFromV158Comment(exit_comment);
   if(v!="") return v;
   return StrategyForMagic(magic);
}

bool HttpPost(string path,string body,string &response){
   string base=CleanBaseUrl(InpWebsiteUrl), url=base+path;
   char data[],result[]; string headers="Content-Type: application/json\r\nX-API-Key: "+InpApiKey+"\r\n"; string rh;
   StringToCharArray(body,data,0,WHOLE_ARRAY,CP_UTF8);
   if(ArraySize(data)>0) ArrayResize(data,ArraySize(data)-1);
   ResetLastError();
   int code=WebRequest("POST",url,headers,InpHttpTimeoutMs,data,result,rh);
   response=CharArrayToString(result,0,-1,CP_UTF8);
   if(code<200 || code>=300){
      Print("Mr Gold Free Sync HTTP error ",code," err=",GetLastError()," response=",response," url=",url);
      return false;
   }
   return true;
}
void SendSnapshot(){
   string typ=(AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO?"demo":"live");
   string body=StringFormat("{\"ts\":\"%s\",\"broker\":\"%s\",\"server\":\"%s\",\"login\":\"%I64d\",\"currency\":\"%s\",\"account_type\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f,\"floating_profit\":%.2f}",
      Iso(TimeGMT()),JsonEscape(AccountInfoString(ACCOUNT_COMPANY)),JsonEscape(AccountInfoString(ACCOUNT_SERVER)),AccountInfoInteger(ACCOUNT_LOGIN),
      JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)),typ,AccountInfoDouble(ACCOUNT_BALANCE),AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN),AccountInfoDouble(ACCOUNT_MARGIN_FREE),AccountInfoDouble(ACCOUNT_PROFIT));
   string r; HttpPost("/api/v1/sync/snapshot",body,r);
}
bool FindEntryForPosition(long pos_id, datetime &ot, double &op, string &side, string &entry_comment){
   int total=HistoryDealsTotal(); bool found=false;
   for(int i=0;i<total;i++){
      ulong tk=HistoryDealGetTicket(i); if(!tk) continue;
      if((long)HistoryDealGetInteger(tk,DEAL_POSITION_ID)!=pos_id) continue;
      long entry=HistoryDealGetInteger(tk,DEAL_ENTRY); if(entry!=DEAL_ENTRY_IN && entry!=DEAL_ENTRY_INOUT) continue;
      datetime t=(datetime)HistoryDealGetInteger(tk,DEAL_TIME);
      if(!found || t<ot){
         ot=t; op=HistoryDealGetDouble(tk,DEAL_PRICE);
         long dt=HistoryDealGetInteger(tk,DEAL_TYPE); side=(dt==DEAL_TYPE_BUY?"BUY":"SELL");
         entry_comment=HistoryDealGetString(tk,DEAL_COMMENT); found=true;
      }
   }
   return found;
}
void FlushTradeBatch(string &arr,int &added){
   if(added<=0) return;
   arr+="]"; string r;
   HttpPost("/api/v1/sync/trades","{\"trades\":"+arr+"}",r);
   arr="["; added=0;
}
void SendClosedTrades(int history_days){
   datetime from=TimeCurrent()-history_days*86400, to=TimeCurrent()+60;
   if(!HistorySelect(from,to)){ Print("HistorySelect failed"); return; }
   int total=HistoryDealsTotal(); string arr="["; int added=0;
   for(int i=0;i<total;i++){
      ulong tk=HistoryDealGetTicket(i); if(!tk) continue;
      long entry=HistoryDealGetInteger(tk,DEAL_ENTRY);
      if(entry!=DEAL_ENTRY_OUT && entry!=DEAL_ENTRY_OUT_BY) continue;
      long type=HistoryDealGetInteger(tk,DEAL_TYPE);
      if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL) continue;

      long pos=(long)HistoryDealGetInteger(tk,DEAL_POSITION_ID);
      long magic=HistoryDealGetInteger(tk,DEAL_MAGIC);
      datetime ct=(datetime)HistoryDealGetInteger(tk,DEAL_TIME), ot=ct;
      double op=0; string side=(type==DEAL_TYPE_BUY?"SELL":"BUY"), entry_comment="";
      FindEntryForPosition(pos,ot,op,side,entry_comment);
      string exit_comment=HistoryDealGetString(tk,DEAL_COMMENT);
      string uid=UniqueIdForTrade(magic,exit_comment,entry_comment);
      double pr=HistoryDealGetDouble(tk,DEAL_PROFIT), co=HistoryDealGetDouble(tk,DEAL_COMMISSION), sw=HistoryDealGetDouble(tk,DEAL_SWAP), fee=HistoryDealGetDouble(tk,DEAL_FEE), net=pr+co+sw+fee;
      string one=StringFormat("{\"deal_ticket\":\"%I64u\",\"position_id\":\"%I64d\",\"magic\":\"%I64d\",\"unique_id\":\"%s\",\"strategy\":\"%s\",\"symbol\":\"%s\",\"side\":\"%s\",\"volume\":%.2f,\"open_price\":%.8f,\"close_price\":%.8f,\"open_time\":\"%s\",\"close_time\":\"%s\",\"profit\":%.2f,\"commission\":%.2f,\"swap\":%.2f,\"fee\":%.2f,\"net_profit\":%.2f,\"comment\":\"%s\"}",
         tk,pos,magic,JsonEscape(uid),JsonEscape(StrategyForTrade(magic,exit_comment,entry_comment)),JsonEscape(HistoryDealGetString(tk,DEAL_SYMBOL)),side,
         HistoryDealGetDouble(tk,DEAL_VOLUME),op,HistoryDealGetDouble(tk,DEAL_PRICE),Iso(ot),Iso(ct),pr,co,sw,fee,net,JsonEscape(exit_comment));
      if(added>0) arr+=","; arr+=one; added++;
      if(added>=100) FlushTradeBatch(arr,added);
   }
   FlushTradeBatch(arr,added);
}
void SyncNow(){
   if(InpSendSnapshots) SendSnapshot();
   if(InpSendClosedTrades){
      int days=(g_first_history_sync?InpInitialHistoryDays:InpRecentHistoryDays);
      SendClosedTrades(days);
      g_first_history_sync=false;
   }
}
int OnInit(){
   int sec=MathMax(60,InpSyncEverySeconds);
   EventSetTimer(sec);
   Print("Mr Gold Free Sync V3 started. Sync=",sec," sec; V1.58 UID auto-detection=",(InpAutoDetectMrGoldV158?"ON":"OFF")," BaseMagic=",InpMrGoldBaseMagic);
   Print("MT5 WebRequest whitelist required: ",CleanBaseUrl(InpWebsiteUrl));
   SyncNow();
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason){ EventKillTimer(); }
void OnTimer(){ SyncNow(); }
