//+------------------------------------------------------------------+
//| HistoryExporter.mq5                                               |
//| Run ONCE manually (drag onto any chart in MT5, click Run) — NOT an |
//| EA, just a Script that exports real historical OHLC from this     |
//| broker's own feed to a JSON file, so the Python backtest engine   |
//| can use the SAME price source as live trading instead of Yahoo    |
//| Finance (a different liquidity provider with no real spread).     |
//| Re-run any time you want a fresher/longer history snapshot.       |
//+------------------------------------------------------------------+
#property script_show_inputs

input string SymbolsCSV = "EURUSD,GBPUSD,USDJPY,XAUUSD,US30,NAS100";
input int    H1Days     = 90;   // ~3 months of H1 bars
input int    M1Days     = 14;   // shorter M1 window — finer resolution, kept short so the file stays a reasonable size
input string OutputFile = "mt5_history.json";

string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
}

// Builds one {"o":[...],"h":[...],"l":[...],"c":[...],"t":[...]} block for
// a symbol/timeframe. Retries a few times because MT5 may need to download
// missing history from the broker server first — CopyRates returns 0 while
// that's in progress, not an error.
string BuildSeriesJson(string symbol, ENUM_TIMEFRAMES period, int days)
{
   datetime fromTime = TimeCurrent() - days * 86400;
   MqlRates rates[];

   int copied = CopyRates(symbol, period, fromTime, TimeCurrent(), rates);
   int attempts = 0;
   while (copied <= 0 && attempts < 10)
   {
      Sleep(1000);
      copied = CopyRates(symbol, period, fromTime, TimeCurrent(), rates);
      attempts++;
   }
   if (copied <= 0)
      return "";

   int symDig = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   string opens = "", highs = "", lows = "", closes = "", times = "";
   for (int j = 0; j < copied; j++)
   {
      string sep = (j < copied - 1) ? "," : "";
      opens  += DoubleToString(rates[j].open,  symDig) + sep;
      highs  += DoubleToString(rates[j].high,  symDig) + sep;
      lows   += DoubleToString(rates[j].low,   symDig) + sep;
      closes += DoubleToString(rates[j].close, symDig) + sep;
      times  += IntegerToString((long)rates[j].time) + sep;
   }

   string json = "{";
   json += "\"o\":[" + opens + "],";
   json += "\"h\":[" + highs + "],";
   json += "\"l\":[" + lows + "],";
   json += "\"c\":[" + closes + "],";
   json += "\"t\":[" + times + "]";
   json += "}";
   return json;
}

void OnStart()
{
   string symbolList[];
   int n = StringSplit(SymbolsCSV, ',', symbolList);

   string entries[];
   ArrayResize(entries, n);
   int found = 0;

   for (int i = 0; i < n; i++)
   {
      string sym = symbolList[i];
      SymbolSelect(sym, true);
      Print("HistoryExporter: exporting ", sym, "...");

      string h1Json = BuildSeriesJson(sym, PERIOD_H1, H1Days);
      string m1Json = BuildSeriesJson(sym, PERIOD_M1, M1Days);

      if (h1Json == "" && m1Json == "")
      {
         Print("HistoryExporter: WARNING — no history available for ", sym, ", skipped");
         continue;
      }

      string entry = "\"" + JsonEscape(sym) + "\":{";
      entry += "\"h1\":" + (h1Json != "" ? h1Json : "null") + ",";
      entry += "\"m1\":" + (m1Json != "" ? m1Json : "null");
      entry += "}";
      entries[found] = entry;
      found++;
   }

   string json = "{";
   json += "\"exported_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"symbols\":{";
   for (int i = 0; i < found; i++)
   {
      json += entries[i];
      if (i < found - 1) json += ",";
   }
   json += "}}";

   int handle = FileOpen(OutputFile, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if (handle != INVALID_HANDLE)
   {
      FileWriteString(handle, json);
      FileClose(handle);
      Print("HistoryExporter: done — wrote ", found, " symbols to Common\\Files\\", OutputFile);
   }
   else
   {
      Print("HistoryExporter: ERROR — could not open output file for writing");
   }
}
