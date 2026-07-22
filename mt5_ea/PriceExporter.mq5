//+------------------------------------------------------------------+
//| PriceExporter.mq5                                                 |
//| Exports account info + watched symbol prices to a JSON file every |
//| second so the Python backend can read real MT5 data without       |
//| linking against the MetaTrader5 Python package (blocked by WDAC). |
//| Also polls a command file and executes trades on it — DEMO-ONLY:  |
//| every command is re-checked against ACCOUNT_TRADE_MODE before any |
//| order is sent, so this refuses to fire on a real account even if  |
//| the EA is later attached to one.                                  |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input string SymbolsCSV = "EURUSD,GBPUSD,USDJPY,XAUUSD,US30,NAS100";
input int    ExportIntervalSec = 1;
input string OutputFile = "trading_room_snapshot.json";
input string CommandFile = "trading_room_command.json";
input string ResultFile = "trading_room_command_result.json";

// --- Position management (partial close + breakeven + trailing) ---
// Mirrors the "TP1/TP2 + adaptive trailing" idea from the
// XAUUSD_Portfolio_v9 EA's auto-disable/management features, but keyed
// off the position's OWN sl/tp (set by Python at order time from real
// ATR) instead of needing a separate ATR handle in this EA.
input bool   InpUsePartialClose   = true;
input double InpPartialCloseRatio = 0.5;  // close this fraction of volume at TP1
input double InpTP1Fraction       = 0.5;  // TP1 = entry + (tp-entry) * this fraction
input bool   InpUseTrailing       = true;
input double InpTrailDistFraction = 0.3;  // trail SL this fraction of (tp-entry) behind price

const long   EA_MAGIC = 20260622;
const string EA_COMMENT = "trading-room-ai";

string symbolList[];
long lastCommandId = 0;
CTrade trade;
ulong  gPartialClosedTickets[];

// H1 multi-timeframe indicator handles, one set per watched symbol —
// created once in OnInit (not per-tick) per MQL5 best practice.
int h1Ema40[];
int h1Ema90[];
int h1Ema100[];
int h1Rsi[];

int OnInit()
{
   int n = StringSplit(SymbolsCSV, ',', symbolList);
   ArrayResize(h1Ema40, n);
   ArrayResize(h1Ema90, n);
   ArrayResize(h1Ema100, n);
   ArrayResize(h1Rsi, n);
   for (int i = 0; i < n; i++)
   {
      SymbolSelect(symbolList[i], true);
      h1Ema40[i]  = iMA(symbolList[i], PERIOD_H1, 40, 0, MODE_EMA, PRICE_CLOSE);
      h1Ema90[i]  = iMA(symbolList[i], PERIOD_H1, 90, 0, MODE_EMA, PRICE_CLOSE);
      h1Ema100[i] = iMA(symbolList[i], PERIOD_H1, 100, 0, MODE_EMA, PRICE_CLOSE);
      h1Rsi[i]    = iRSI(symbolList[i], PERIOD_H1, 14, PRICE_CLOSE);
   }

   EventSetTimer(ExportIntervalSec);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   for (int i = 0; i < ArraySize(symbolList); i++)
   {
      IndicatorRelease(h1Ema40[i]);
      IndicatorRelease(h1Ema90[i]);
      IndicatorRelease(h1Ema100[i]);
      IndicatorRelease(h1Rsi[i]);
   }
}

void OnTimer()
{
   ExportSnapshot();
   CheckCommand();
   ManageOpenPositions();
}

string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
}

string TradeModeString()
{
   long mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if (mode == ACCOUNT_TRADE_MODE_DEMO) return "demo";
   if (mode == ACCOUNT_TRADE_MODE_CONTEST) return "contest";
   return "real";
}

void ExportSnapshot()
{
   string json = "{";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";

   json += "\"account\":{";
   json += "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   json += "\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   json += "\"profit\":" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2) + ",";
   json += "\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
   json += "\"currency\":\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)) + "\",";
   json += "\"trade_mode\":\"" + TradeModeString() + "\"";
   json += "},";

   string symbolEntries[];
   int n = ArraySize(symbolList);
   int found = 0;
   ArrayResize(symbolEntries, n);
   for (int i = 0; i < n; i++)
   {
      string sym = symbolList[i];
      MqlTick tick;
      if (!SymbolInfoTick(sym, tick))
         continue;
      int symDig = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      string entry = "\"" + JsonEscape(sym) + "\":{";
      entry += "\"bid\":" + DoubleToString(tick.bid, symDig) + ",";
      entry += "\"ask\":" + DoubleToString(tick.ask, symDig) + ",";
      entry += "\"time\":" + IntegerToString((long)tick.time);
      entry += "}";
      symbolEntries[found] = entry;
      found++;
   }
   json += "\"symbols\":{";
   for (int i = 0; i < found; i++)
   {
      json += symbolEntries[i];
      if (i < found - 1) json += ",";
   }
   json += "},";

   json += "\"candles\":{" + BuildCandlesJson() + "},";
   json += "\"h1\":{" + BuildH1Json() + "},";
   json += "\"h1_candles\":{" + BuildH1CandlesJson() + "},";

   string positionEntries[];
   int total = PositionsTotal();
   ArrayResize(positionEntries, total);
   int posFound = 0;
   for (int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      double vol = PositionGetDouble(POSITION_VOLUME);
      double profit = PositionGetDouble(POSITION_PROFIT);
      long type = PositionGetInteger(POSITION_TYPE);
      string entry = "{\"ticket\":" + IntegerToString((long)ticket) + ",";
      entry += "\"symbol\":\"" + JsonEscape(sym) + "\",";
      entry += "\"volume\":" + DoubleToString(vol, 2) + ",";
      entry += "\"profit\":" + DoubleToString(profit, 2) + ",";
      entry += "\"type\":" + (type == POSITION_TYPE_BUY ? "\"buy\"" : "\"sell\"");
      entry += "}";
      positionEntries[posFound] = entry;
      posFound++;
   }
   json += "\"positions\":[";
   for (int i = 0; i < posFound; i++)
   {
      json += positionEntries[i];
      if (i < posFound - 1) json += ",";
   }
   json += "]";
   json += "}";

   int handle = FileOpen(OutputFile, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if (handle != INVALID_HANDLE)
   {
      FileWriteString(handle, json);
      FileClose(handle);
   }
}

double GetBuf(const int handle, const int buffer, const int shift)
{
   double b[];
   if (CopyBuffer(handle, buffer, shift, 1, b) <= 0) return EMPTY_VALUE;
   return b[0];
}

// --- Real H1 trend per symbol (EMA40/90/100 + RSI14) — genuine
// multi-timeframe confirmation, same idea as the "Eddard"/"JonSnow"
// strategies' H1 trend filter, instead of the EMA50-on-M1 approximation.
string BuildH1Json()
{
   string result = "";
   int n = ArraySize(symbolList);
   for (int i = 0; i < n; i++)
   {
      double e40 = GetBuf(h1Ema40[i], 0, 1);
      double e90 = GetBuf(h1Ema90[i], 0, 1);
      double e100 = GetBuf(h1Ema100[i], 0, 1);
      double rsi = GetBuf(h1Rsi[i], 0, 1);
      if (e40 == EMPTY_VALUE || e90 == EMPTY_VALUE || e100 == EMPTY_VALUE || rsi == EMPTY_VALUE)
         continue;

      int symDig = (int)SymbolInfoInteger(symbolList[i], SYMBOL_DIGITS);
      if (result != "") result += ",";
      result += "\"" + JsonEscape(symbolList[i]) + "\":{";
      result += "\"ema40\":" + DoubleToString(e40, symDig) + ",";
      result += "\"ema90\":" + DoubleToString(e90, symDig) + ",";
      result += "\"ema100\":" + DoubleToString(e100, symDig) + ",";
      result += "\"rsi\":" + DoubleToString(rsi, 2) + ",";
      result += "\"trend\":\"" + ((e40 > e90 && e90 > e100) ? "up" : (e40 < e90 && e90 < e100) ? "down" : "mixed") + "\"";
      result += "}";
   }
   return result;
}

// --- Real M1 candle OHLC, last 60 bars per watched symbol — replaces the
// Python-side mock high/low (price*1.0005 etc) so pin_bar/engulfing/ATR
// are computed from actual candles instead of a fake spread around tick price.
string BuildCandlesJson()
{
   string result = "";
   int n = ArraySize(symbolList);
   for (int i = 0; i < n; i++)
   {
      string sym = symbolList[i];
      MqlRates rates[];
      // 300 M1 bars: enough to resample into M5 (60 bars) and M15 (20
      // bars) for the multi-timeframe engine's lower-TF entry pairs.
      int copied = CopyRates(sym, PERIOD_M1, 0, 300, rates);
      if (copied <= 0)
         continue;
      int symDig = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

      string opens = "", highs = "", lows = "", closes = "";
      for (int j = 0; j < copied; j++)
      {
         string sep = (j < copied - 1) ? "," : "";
         opens += DoubleToString(rates[j].open, symDig) + sep;
         highs += DoubleToString(rates[j].high, symDig) + sep;
         lows += DoubleToString(rates[j].low, symDig) + sep;
         closes += DoubleToString(rates[j].close, symDig) + sep;
      }

      if (result != "") result += ",";
      result += "\"" + JsonEscape(sym) + "\":{";
      result += "\"o\":[" + opens + "],";
      result += "\"h\":[" + highs + "],";
      result += "\"l\":[" + lows + "],";
      result += "\"c\":[" + closes + "]";
      result += "}";
   }
   return result;
}

// --- Real H1 candle OHLC, last 150 bars per watched symbol — enough
// history for SMC swing structure and Elliott Wave zigzag pivots to
// mean something (60 M1 bars is only ~1 hour of price action, nowhere
// near enough to count waves or find meaningful structure).
string BuildH1CandlesJson()
{
   string result = "";
   int n = ArraySize(symbolList);
   for (int i = 0; i < n; i++)
   {
      string sym = symbolList[i];
      MqlRates rates[];
      int copied = CopyRates(sym, PERIOD_H1, 0, 150, rates);
      if (copied <= 0)
         continue;
      int symDig = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

      string opens = "", highs = "", lows = "", closes = "";
      for (int j = 0; j < copied; j++)
      {
         string sep = (j < copied - 1) ? "," : "";
         opens += DoubleToString(rates[j].open, symDig) + sep;
         highs += DoubleToString(rates[j].high, symDig) + sep;
         lows += DoubleToString(rates[j].low, symDig) + sep;
         closes += DoubleToString(rates[j].close, symDig) + sep;
      }

      if (result != "") result += ",";
      result += "\"" + JsonEscape(sym) + "\":{";
      result += "\"o\":[" + opens + "],";
      result += "\"h\":[" + highs + "],";
      result += "\"l\":[" + lows + "],";
      result += "\"c\":[" + closes + "]";
      result += "}";
   }
   return result;
}

// --- Reads a JSON string value for a given key from a flat (non-nested) object ---
string JsonGetString(string json, string key)
{
   string needle = "\"" + key + "\":\"";
   int start = StringFind(json, needle);
   if (start < 0) return "";
   start += StringLen(needle);
   int end = StringFind(json, "\"", start);
   if (end < 0) return "";
   return StringSubstr(json, start, end - start);
}

double JsonGetNumber(string json, string key)
{
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if (start < 0) return 0;
   start += StringLen(needle);
   int end = start;
   while (end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if ((c >= '0' && c <= '9') || c == '.' || c == '-') end++;
      else break;
   }
   return StringToDouble(StringSubstr(json, start, end - start));
}

void WriteResult(long id, bool success, string message, ulong ticket,
                  double requestedPrice = 0, double filledPrice = 0,
                  double commission = 0, double swap = 0)
{
   string json = "{";
   json += "\"id\":" + IntegerToString(id) + ",";
   json += "\"success\":" + (success ? "true" : "false") + ",";
   json += "\"message\":\"" + JsonEscape(message) + "\",";
   json += "\"ticket\":" + IntegerToString((long)ticket) + ",";
   json += "\"requested_price\":" + DoubleToString(requestedPrice, 6) + ",";
   json += "\"filled_price\":" + DoubleToString(filledPrice, 6) + ",";
   json += "\"slippage\":" + DoubleToString(filledPrice - requestedPrice, 6) + ",";
   json += "\"commission\":" + DoubleToString(commission, 2) + ",";
   json += "\"swap\":" + DoubleToString(swap, 2);
   json += "}";
   int handle = FileOpen(ResultFile, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if (handle != INVALID_HANDLE)
   {
      FileWriteString(handle, json);
      FileClose(handle);
   }
}

// --- Partial-close tracking — a plain array of tickets already closed
// at TP1, so we don't repeat the partial close every timer tick. ---
bool WasPartialClosed(const ulong ticket)
{
   for (int i = 0; i < ArraySize(gPartialClosedTickets); i++)
      if (gPartialClosedTickets[i] == ticket) return true;
   return false;
}

void MarkPartialClosed(const ulong ticket)
{
   int n = ArraySize(gPartialClosedTickets);
   ArrayResize(gPartialClosedTickets, n + 1);
   gPartialClosedTickets[n] = ticket;
}

// Drops tickets for positions that no longer exist, so the array doesn't
// grow forever over a long-running session.
void PruneClosedTickets()
{
   ulong fresh[];
   int n = ArraySize(gPartialClosedTickets);
   for (int i = 0; i < n; i++)
   {
      if (PositionSelectByTicket(gPartialClosedTickets[i]))
      {
         int sz = ArraySize(fresh);
         ArrayResize(fresh, sz + 1);
         fresh[sz] = gPartialClosedTickets[i];
      }
   }
   ArrayResize(gPartialClosedTickets, ArraySize(fresh));
   for (int i = 0; i < ArraySize(fresh); i++) gPartialClosedTickets[i] = fresh[i];
}

// --- Manage every open position this EA placed: partial-close at TP1
// (halfway to the original take-profit) + move SL to breakeven, then
// trail the stop behind price as it keeps moving favorably. Both use
// the position's OWN sl/tp (set by Python from real ATR at entry time)
// instead of needing a separate ATR indicator handle here — same idea
// as XAUUSD_Portfolio_v9's TP1/TP2 + adaptive trailing, simplified to
// not require recomputing volatility in MQL5.
void ManageOpenPositions()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (!PositionSelectByTicket(ticket)) continue;
      if (PositionGetInteger(POSITION_MAGIC) != EA_MAGIC) continue;
      if (PositionGetString(POSITION_COMMENT) != EA_COMMENT) continue;

      string symbol   = PositionGetString(POSITION_SYMBOL);
      long   type      = PositionGetInteger(POSITION_TYPE);
      double entry     = PositionGetDouble(POSITION_PRICE_OPEN);
      double curSL     = PositionGetDouble(POSITION_SL);
      double curTP     = PositionGetDouble(POSITION_TP);
      double volume    = PositionGetDouble(POSITION_VOLUME);
      bool   isBuy      = (type == POSITION_TYPE_BUY);

      if (curTP == 0) continue;  // no TP recorded — nothing to base TP1/trail on
      double fullDist = MathAbs(curTP - entry);
      if (fullDist <= 0) continue;

      MqlTick tick;
      if (!SymbolInfoTick(symbol, tick)) continue;
      double curPrice = isBuy ? tick.bid : tick.ask;

      int symDigits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

      // --- Partial close at TP1 + move to breakeven ---
      if (InpUsePartialClose && !WasPartialClosed(ticket))
      {
         double tp1Price = isBuy ? entry + fullDist * InpTP1Fraction
                                  : entry - fullDist * InpTP1Fraction;
         bool reachedTP1 = isBuy ? (curPrice >= tp1Price) : (curPrice <= tp1Price);

         if (reachedTP1)
         {
            double closeVolume = NormalizeDouble(volume * InpPartialCloseRatio, 2);
            double volMin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
            if (closeVolume >= volMin && closeVolume < volume)
            {
               if (trade.PositionClosePartial(ticket, closeVolume))
               {
                  MarkPartialClosed(ticket);
                  double beSL = NormalizeDouble(entry, symDigits);
                  trade.PositionModify(ticket, beSL, curTP);
                  Print("ManageOpenPositions: partial close + breakeven on ticket ", ticket);
               }
            }
            else
            {
               // Lot too small to split — just mark it so we don't keep
               // re-checking, and move to breakeven anyway.
               MarkPartialClosed(ticket);
               trade.PositionModify(ticket, NormalizeDouble(entry, symDigits), curTP);
            }
         }
      }

      // --- Trailing stop — only once price has moved favorably past
      // breakeven by at least the trail distance, and only ever tightens
      // (never loosens) the stop. ---
      if (InpUseTrailing)
      {
         double trailDist = fullDist * InpTrailDistFraction;
         if (trailDist > 0)
         {
            if (isBuy)
            {
               double newSL = curPrice - trailDist;
               if (curPrice > entry + trailDist && (curSL == 0 || newSL > curSL))
                  trade.PositionModify(ticket, NormalizeDouble(newSL, symDigits), curTP);
            }
            else
            {
               double newSL = curPrice + trailDist;
               if (curPrice < entry - trailDist && (curSL == 0 || newSL < curSL))
                  trade.PositionModify(ticket, NormalizeDouble(newSL, symDigits), curTP);
            }
         }
      }
   }

   PruneClosedTickets();
}

void CheckCommand()
{
   if (!FileIsExist(CommandFile, FILE_COMMON))
      return;

   int handle = FileOpen(CommandFile, FILE_READ|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if (handle == INVALID_HANDLE)
      return;

   string json = "";
   while (!FileIsEnding(handle))
      json += FileReadString(handle);
   FileClose(handle);

   long id = (long)JsonGetNumber(json, "id");
   if (id == lastCommandId || id == 0)
      return;
   lastCommandId = id;

   // Hard safety check — refuse on anything that isn't a demo account,
   // no matter what the command says.
   if (TradeModeString() != "demo")
   {
      WriteResult(id, false, "refused: account is not a demo account", 0);
      return;
   }

   string symbol = JsonGetString(json, "symbol");
   string action = JsonGetString(json, "action");
   double riskMoney = JsonGetNumber(json, "risk_money");
   double sl = JsonGetNumber(json, "sl");
   double tp = JsonGetNumber(json, "tp");

   if (symbol == "" || (action != "buy" && action != "sell"))
   {
      WriteResult(id, false, "invalid command payload", 0);
      return;
   }

   MqlTick tick;
   if (!SymbolInfoTick(symbol, tick))
   {
      WriteResult(id, false, "symbol tick unavailable", 0);
      return;
   }
   double entry = (action == "buy") ? tick.ask : tick.bid;

   // Broker-enforced minimum distance between entry and SL/TP. Sending
   // stops tighter than this returns retcode 10016 (Invalid Stops) and
   // silently drops the trade — pad out to the minimum (+20% buffer)
   // instead of just rejecting, since the ATR-based SL/TP from Python
   // can legitimately come in tighter than this on quiet symbols.
   long stopsLevelPoints = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double symPoint = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double minStopDistance = stopsLevelPoints * symPoint * 1.2;
   if (minStopDistance > 0)
   {
      double slDist = MathAbs(entry - sl);
      double tpDist = MathAbs(entry - tp);
      if (slDist < minStopDistance)
         sl = (action == "buy") ? entry - minStopDistance : entry + minStopDistance;
      if (tpDist < minStopDistance)
         tp = (action == "buy") ? entry + minStopDistance : entry - minStopDistance;
   }

   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double slDistance = MathAbs(entry - sl);
   if (tickSize <= 0 || slDistance <= 0)
   {
      WriteResult(id, false, "cannot size position (zero tick size or sl distance)", 0);
      return;
   }
   double lossPerLot = (slDistance / tickSize) * tickValue;
   if (lossPerLot <= 0)
   {
      WriteResult(id, false, "cannot size position (zero loss per lot)", 0);
      return;
   }
   double lot = riskMoney / lossPerLot;

   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double lotMin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double lotMax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   lot = MathFloor(lot / lotStep) * lotStep;
   lot = MathMax(lotMin, MathMin(lotMax, lot));

   int symDigits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   sl = NormalizeDouble(sl, symDigits);
   tp = NormalizeDouble(tp, symDigits);

   trade.SetExpertMagicNumber(EA_MAGIC);
   bool ok;
   if (action == "buy")
      ok = trade.Buy(lot, symbol, 0, sl, tp, EA_COMMENT);
   else
      ok = trade.Sell(lot, symbol, 0, sl, tp, EA_COMMENT);

   if (!ok)
   {
      WriteResult(id, false, "OrderSend failed: " + IntegerToString(trade.ResultRetcode()), 0);
      return;
   }

   double filledPrice = trade.ResultPrice();
   double commission = 0, swap = 0;
   ulong dealTicket = trade.ResultDeal();
   if (dealTicket > 0 && HistoryDealSelect(dealTicket))
   {
      commission = HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
      swap = HistoryDealGetDouble(dealTicket, DEAL_SWAP);
   }

   WriteResult(id, true, "executed", trade.ResultOrder(), entry, filledPrice, commission, swap);
}
