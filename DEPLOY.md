# Deploy บน VPS Windows (mt4.cloud) — ลองจริง 1 เดือน

ระบบพร้อม deploy แล้ว เหลือแค่ทำตามขั้นตอนนี้

## สรุปค่าใช้จ่าย
- VPS Windows (Extra 2 vCore 2GB): ~390 บาท/เดือน
- Gemini API (เปิด billing + quota limit): ~90-200 บาท/เดือน
- **รวม ~500-590 บาท/เดือน**

---

## ขั้นตอน

### 1. เตรียม Gemini API
1. ไป https://aistudio.google.com/apikey → Create API key
2. เปิด billing ที่ https://console.cloud.google.com/billing
3. ตั้ง Quota limit (APIs & Services → Quotas → Generative Language API)
   → จำกัด ~4-5 ล้าน token/วัน กันบิลเกิน ~600 บาท

### 2. เช่า VPS
1. mt4.cloud → Extra Package (หรือขอทดลองฟรี 7 วันก่อน)
2. เลือก Windows + location ใกล้ broker (SG/UK)
3. รับ IP + รหัส RDP

### 3. ติดตั้งบน VPS (ผ่าน RDP)
```powershell
# ติดตั้ง Python 3.11+ จาก python.org (ติ๊ก "Add to PATH")
# ติดตั้ง MT5 + login demo Vantage

git clone <YOUR_GITHUB_REPO_URL>
cd AgentAI\backend
pip install -r requirements.txt   # ได้ MetaTrader5 มาด้วย (Windows)
```

### 4. ตั้งค่า .env (บน VPS)
สร้าง `backend\.env` (ดู .env.example):
```
GEMINI_API_KEY=<key จริง>
GROQ_API_KEY=<key ฟรี>
CEREBRAS_API_KEY=<key ฟรี>
DASHBOARD_PASSWORD=<รหัสหน้าเว็บของคุณ>
```

### 5. เปิด MT5 + login demo ค้างไว้
- MT5 Python จะเชื่อมตรง (ไม่ต้องใส่ EA!)
- เช็ค: เปิดเว็บ → /account ควรเห็น `source: "mt5_direct"`

### 6. รันระบบ (auto-restart)
```
ดับเบิลคลิก start_system.bat
```
หรือใส่ Task Scheduler "At log on" ให้ฟื้นเองหลัง reboot

### 7. เปิดดูจากที่ไหนก็ได้
```
http://<IP_VPS>:5500   (ใส่รหัสที่ตั้งใน DASHBOARD_PASSWORD)
```
เปิด firewall port 5500 + 8000 บน VPS

---

## เช็คว่าทำงานถูก
- `/account` → source: "mt5_direct", live: true, บัญชี demo ✅
- แชท → เห็น agent วิเคราะห์ + 📋 ใบตรวจสอบ
- เมื่อมีไม้จริง → มี ticket #, ปฏิทินเริ่มมีข้อมูล

## หลัง deploy — เก็บข้อมูล 2-4 สัปดาห์
ดู **expectancy** (ไม่ใช่ win rate):
- บวกจริง → พิจารณาเงินจริง
- ลบ → ปรับ strategy (ยังไม่เสียเงินเทรด)
