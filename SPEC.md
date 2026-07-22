# Multi-Agent Trading Assistant — Spec (Draft v0.1)

## ขอบเขตสำคัญ (ต้องอ่านก่อน)
- ระบบนี้ผลิต **signal + คำแนะนำ** เท่านั้น
- **ไม่มี auto-execute คำสั่งเทรดจริงบน MT4/MT5 บัญชี FTMO** — ทุกไม้ต้องมีคนกดยืนยันเอง
- เป้าหมาย: ลดเวลาการวิเคราะห์ ไม่ใช่แทนที่การตัดสินใจของผู้เทรด

## บัญชี/ตลาด
- บัญชี: FTMO (Challenge/Funded — ระบุ phase ปัจจุบันเมื่อรู้ drawdown rule ของรอบนั้น)
- สินทรัพย์: Forex, Gold/Commodities, Indices (ทั้งหมด — ต้อง config ต่อ symbol ได้)
- Timeframe: M1/M5 (เลือกตามจังหวะตลาด ไม่ fix ตัวเดียว)
- Data source: MT4/MT5 ผ่าน broker FTMO (ราคาตรงกับที่ใช้เทรดจริง)

## สถาปัตยกรรม Agent

```
                         [CEO Agent / Orchestrator]
                        /         |          \
          [Technical Analysis] [News/Fund] [Risk Management]
                  |
        [Knowledge Base: PDF (RAG) + Web Search]
                        |
                  [Notifier Agent] → Telegram/Discord
                        |
                  [Journal/Logger Agent]
```

### 1. Data Agent
- ดึง OHLCV + tick data จาก MT4/MT5 (ผ่าน MetaTrader Python bridge เช่น `MetaTrader5` package หรือ EA ที่ export ข้อมูลออกมาทาง socket/file)
- Normalize เป็น format กลาง (timestamp, OHLC, volume, symbol, timeframe) ส่งต่อให้ agent อื่น

### 2. Technical Analysis Agent
- คำนวณ indicator (EMA, RSI, MACD, S/R, price action pattern) บน M1/M5
- **Knowledge ingestion**: รับ PDF (ตำรา/สไตล์เทรดของผู้ใช้) → เก็บเป็น vector embeddings (RAG) ใน vector DB (เช่น Chroma/Qdrant) → agent ค้น context ก่อนวิเคราะห์
- **Web research**: มี web search tool ติดตัว ค้นข้อมูลเพิ่มเติม (เช่น sentiment, setup ที่กำลังเทรนด์) — ใช้เสริมเท่านั้น ไม่ใช่ source หลักของ decision
- Output: setup ที่พบ + confidence score + เหตุผลอ้างอิง (จาก RAG หรือ indicator)

### 3. News/Fundamental Agent
- เช็ค economic calendar (high-impact news ตรงกับ symbol ที่กำลังดู)
- เตือน "ห้ามเทรด" ในช่วง news เวลาที่กำหนด (เช่น ±15 นาทีจาก NFP, CPI, FOMC)

### 4. Risk Management Agent — **มีอำนาจ veto สูงสุด**
- คำนวณ position size จาก % risk ต่อไม้ (เช่น 0.5-1%)
- เช็ค daily loss limit / overall drawdown ตามกฎ FTMO ของ phase ปัจจุบัน
- ปฏิเสธ signal ทันทีถ้าเสี่ยงเกินกำหนด — **CEO Agent ห้าม override กฎนี้เด็ดขาด**
- ต้องมี state การติดตาม equity/drawdown แบบ real-time (ไม่ใช่คำนวณครั้งเดียวต่อ session)

### 5. CEO Agent (Orchestrator)
- รวบรวมรายงานจากทุก agent (structured output: JSON — มุมมอง, confidence, เหตุผล)
- ตัดสินใจสุดท้าย: ออก signal หรือไม่ + รายละเอียด (entry, SL, TP, lot size)
- กฎการตัดสินใจ: Risk Agent reject → จบ, ไม่ส่งต่อ ไม่ว่า Technical/News จะมั่นใจแค่ไหน

### 6. Notifier Agent
- ส่ง alert ไปยัง Telegram/Discord พร้อมสรุปเหตุผลจากทุกฝ่าย ให้ผู้ใช้กดยืนยันเองใน MT4/MT5

### 7. Journal/Logger Agent
- บันทึกทุก signal ที่ออก (ไม่ว่าจะถูกเทรดจริงหรือไม่) + ผลลัพธ์จริงถ้ามี feedback
- ใช้สำหรับ backtest/ปรับ strategy ย้อนหลัง

## เทคโนโลยี (รอ confirm)
- ภาษา: Python (เหมาะกับ `MetaTrader5` package, data/ML ecosystem)
- Multi-agent framework: LangGraph (ควบคุม flow แบบ orchestrator-worker ได้ดี ไม่ใช่ peer-to-peer อิสระไม่จำกัด)
- Vector DB: Chroma (เริ่มง่าย, local-first)
- Notification: Telegram Bot API

## คำถามที่ยังต้องตอบก่อนเริ่มโค้ด
1. FTMO phase ปัจจุบัน (Challenge step 1/2 หรือ Funded) และ % drawdown limit ที่ใช้จริง?
2. มี PDF/ตำราที่จะให้ Technical Agent เรียนรู้แล้วหรือยัง (ไฟล์อะไร กี่ไฟล์)?
3. ต้องการรันบนเครื่องไหน — เครื่องเดียวกับที่เปิด MT4/MT5 อยู่ หรือ server แยก?
4. ความถี่ในการ run analysis loop (เช็คทุกกี่วินาที/นาที)?
