import pandas as pd
import os

file_path = r"D:\我才不要走量化\etf_換股\前20大etf_202601_03.xlsx"
output_path = r"D:\我才不要走量化\etf_換股\前20大etf_unique_list.xlsx"

if not os.path.exists(file_path):
    print(f"找不到檔案: {file_path}")
else:
    try:
        df = pd.read_excel(file_path)

        print("原始資料前 5 筆：")
        print(df.head())

        # 確保 etf_code 轉為字串並補足 5 位 (如 919 -> 00919)
        df['etf_code'] = df['etf_code'].apply(lambda x: str(int(float(x))).zfill(5))

        result = df.groupby('stock_code')['etf_code'].apply(
            lambda x: ' '.join(sorted(x.unique()))
        ).reset_index()

        result.columns = ['stock_code', 'etf_list']

        result.to_excel(output_path, index=False)
        
        print(f"\n處理完成！結果已儲存至: {output_path}")
        print("結果預覽：")
        print(result.head())

    except Exception as e:
        print(f"發生錯誤: {e}")
###############################################################################
# 連接 永豐api + 實測系統
###############################################################################
import shioaji as sj
import pandas as pd
from datetime import datetime
import datetime as dt
import time
import threading
import yfinance as yf
import smtplib
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, jsonify, render_template_string
from shioaji import TickSTKv1, Exchange, constant
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, ".env"))

app = Flask(__name__)

tick_store = {}
stock_etf_map = {} # 儲存股票代號對應的 ETF 清單
stock_prev_chg_map = {} # 儲存個股昨日漲跌幅
triggered_codes = set() # 儲存 13:20 觸發訊號的股票代碼
us_market_info = {"nasdaq_chg": 0.0, "vix_chg": 0.0, "vix_price": 0.0, "date": ""}
alert_sent = False  # 確保每天只發送一次通知

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")
recipient_str = os.getenv('RECIPIENT_EMAILS') #可以放多個mail，要去env改
RECIPIENT_EMAIL = [email.strip() for email in recipient_str.split(',')] if recipient_str else []

api = sj.Shioaji(simulation=False) 

print("系統登入中...")
# 這裡填入您的 API Key 與 Secret Key
api.login(
 api_key=os.getenv("SHIOAJI_API_KEY"), 
 secret_key=os.getenv("SHIOAJI_SECRET_KEY"),
 contracts_timeout=10000,
    )

print("登入成功，合約下載完成。")

# ==========================================
# 1.5 抓取美股資料與郵件功能
# ==========================================
def fetch_us_market_data():
    global us_market_info
    try:
        print("正在抓取美股前一交易日資料...")
        nasdaq = yf.Ticker("^IXIC").history(period="2d")
        vix = yf.Ticker("^VIX").history(period="2d")
        
        if len(nasdaq) >= 2:
            n_close = nasdaq['Close'].iloc[-1]
            n_prev = nasdaq['Close'].iloc[-2]
            us_market_info["nasdaq_chg"] = round(((n_close - n_prev) / n_prev) * 100, 2)
            us_market_info["date"] = nasdaq.index[-1].strftime('%Y/%m/%d') # 改為 yyyy/mm/dd
            
        if len(vix) >= 2:
            v_close = vix['Close'].iloc[-1]
            v_prev = vix['Close'].iloc[-2]
            us_market_info["vix_price"] = round(v_close, 2)
            us_market_info["vix_chg"] = round(((v_close - v_prev) / v_prev) * 100, 2)
            
        print(f"Nasdaq 漲跌: {us_market_info['nasdaq_chg']}% | VIX: {us_market_info['vix_price']}")
    except Exception as e:
        print(f"抓取美股資料失敗: {e}")

def fetch_taiwan_stock_prev_chg(contracts):
    """使用永豐 API 抓取台股標的 T-2 收盤到 T-1 13:20 的漲跌幅"""
    global stock_prev_chg_map
    print("正在從永豐 API 抓取個股 T-1 13:20 vs T-2 收盤資料...")
    
    # 設定抓取範圍：過去 10 天以確保包含足夠交易日
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    today = datetime.now().date()

    try:
        for contract in contracts:
            try:
                # 抓取 1 分 K 線
                kbars = api.kbars(contract, start=start_date, end=end_date)
                df = pd.DataFrame({**kbars})
                if df.empty:
                    continue
                
                df['ts'] = pd.to_datetime(df['ts'])
                df['date'] = df['ts'].dt.date
                
                # 取得所有交易日並排除今天
                trading_days = sorted([d for d in df['date'].unique() if d < today])
                if len(trading_days) < 2:
                    continue
                
                t_minus_1 = trading_days[-1]  # 昨天 (T-1)
                t_minus_2 = trading_days[-2]  # 前天 (T-2)
                
                # 1. T-2 收盤價 (取當天最後一根 K 的 Close)
                p_t2_close = df[df['date'] == t_minus_2]['Close'].iloc[-1]
                
                # 2. T-1 13:20 價格 (取當天 13:20 以前最後一根 K)
                t1_mask = (df['date'] == t_minus_1) & (df['ts'].dt.time <= dt.time(13, 20))
                t1_data = df[t1_mask]
                
                if not t1_data.empty:
                    p_t1_1320 = t1_data['Close'].iloc[-1]
                    # 計算漲跌幅
                    stock_prev_chg_map[contract.code] = round(((p_t1_1320 / p_t2_close) - 1) * 100, 2)
            except Exception as e:
                print(f"抓取 {contract.code} 歷史資料失敗: {e}")
                continue
    except Exception as e:
        print(f"執行歷史漲跌計算時發生錯誤: {e}")

def send_strategy_alert(stocks):
    try:
        subject = f"⚠️ 策略訊號觸發：美股大跌後之強勢股監控 ({us_market_info['date']})"
        body = f"美股 Nasdaq 昨日跌幅達 {us_market_info['nasdaq_chg']}%，觸發監控條件。\n\n"
        body += "以下股票在 13:20 漲跌幅介於 7% ~ 9.5%：\n"
        for s in stocks:
            body += f"- {s['code']}: 漲跌幅 {s['pct_chg']}% (成交價: {s['close']})\n"
        
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = ", ".join(RECIPIENT_EMAIL)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        print("✅ Gmail 通知已發送")
    except Exception as e:
        print(f"❌ 郵件發送失敗: {e}")

fetch_us_market_data()

# ==========================================
# 2. 定義 Callback: 抓取您指定的 13 個欄位
# ==========================================
@api.on_tick_stk_v1()
def quote_callback(exchange: Exchange, tick: TickSTKv1):
    # 將 Tick 資料整理成 Dictionary
    tick_data = {
        "code": tick.code,                              # 商品代碼
        "datetime": tick.datetime.strftime('%H:%M:%S.%f'), # 時間 (轉字串方便閱讀)
        "open": float(tick.open),                       # 開盤價
        "avg_price": float(tick.avg_price),             # 均價
        "close": float(tick.close),                     # 成交價
        "volume": int(tick.volume),                     # 成交量
        "tick_type": int(tick.tick_type),               # 內外盤別
        "pct_chg": float(tick.pct_chg),                 # 漲跌幅
        "bid_side_total_vol": int(tick.bid_side_total_vol), # 買盤成交總量
        "ask_side_total_vol": int(tick.ask_side_total_vol), # 賣盤成交總量
        "closing_oddlot_shares": int(tick.closing_oddlot_shares), # 盤後零股
        "fixed_trade_vol": int(tick.fixed_trade_vol),   # 定盤成交量
    }
    
    # 更新全域儲存空間
    tick_store[tick.code] = tick_data
    
    # 終端機僅保留簡單提示，避免洗版
    # print(f"接收到更新: {tick_data['code']} @ {tick_data['close']}")

api.quote.set_on_tick_stk_v1_callback(quote_callback)

# ==========================================
# 2.5 Flask 網頁介面設定
# ==========================================
@app.route('/')
def index():
    # 簡單的 HTML 模板，使用 Bootstrap 讓介面變漂亮
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Overnight stock  return ETF Pool</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
        <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
        <style>
            body { background-color: #f8f9fa; padding: 20px; font-family: "Times New Roman", Times, serif; }
            .table-container { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin: 0 auto; max-width: 95%; }
            .up-price { color: #ff4d4d !important; font-weight: bold; } /* 鮮豔紅 */
            .down-price { color: #28a745 !important; font-weight: bold; } /* 鮮豔綠 */
            #tick-table, #tick-table thead, #tick-table tbody, #tick-table tr, #tick-table th, #tick-table td { text-align: center !important; vertical-align: middle !important; }
            #tick-table thead th { background-color: lightsteelblue !important; color: #333; }
            .us-market-card { font-size: 1.1rem; min-width: 250px; }
            #tick-table { font-size: 1.2rem !important; width: 100%; } /* 縮小字體至舒適範圍 */
            .triggered-row td { background-color: #fff9c4 !important; } /* 觸發訊號的淺黃色標記 */
            h2 { font-weight: bold; font-size: 2rem; text-align: center; margin-bottom: 20px; }
            .table { margin-bottom: 0; }
        </style>
    </head>
    <body>
        <div class="container-fluid">
            <div class="row mb-4">
                <div class="col-md-8">
                    <h2> overnight stock ETF pool </h2>
                </div>
                <div class="col-md-4 text-end">
                    <div class="card p-2 bg-dark text-white text-center us-market-card">
                        <div class="fw-bold"><span id="us-date">----/--/--</span></div>
                        <div>Nasdaq <span id="us-nasdaq">--</span> | VIX <span id="us-vix">--</span></div>
                    </div>
                </div>
            </div>

            <div class="row justify-content-center">
                <div class="col-11">
                    <div class="table-container">
                        <table class="table table-hover text-center" id="tick-table">
                            <thead>
                                <tr>
                                    <th>代碼</th><th>時間</th><th>成交價</th><th>漲跌幅%</th><th>昨日截至13:20漲跌%</th>
                                    <th>成交量</th><th>開盤</th><th>均價</th><th>包含 ETF</th>
                                </tr>
                            </thead>
                            <tbody id="data-body">
                                <!-- 資料會由 JS 動態填入 -->
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
        <script>
            function updateData() {
                $.getJSON('/data', function(data) {
                    // 更新美股資訊
                    let us = data.us_info;
                    $('#us-date').text(us.date);
                    $('#us-nasdaq').text(us.nasdaq_chg + '%').css('color', us.nasdaq_chg >= 0 ? '#ff4d4d' : '#00ff00');
                    $('#us-vix').text(us.vix_price + ' (' + us.vix_chg + '%)');

                    let rows = '';
                    data.ticks.forEach(function(item) {
                        // 判斷是否為觸發訊號的行
                        let rowClass = item.is_triggered ? 'triggered-row' : '';
                        // 判斷漲跌顏色與符號
                        let priceClass = '';
                        let prefix = '';
                        if (item.pct_chg > 0) {
                            priceClass = 'up-price';
                            prefix = '+';
                        } else if (item.pct_chg < 0) {
                            priceClass = 'down-price';
                        }
                        
                        let displayPct = prefix + item.pct_chg.toFixed(2) + '%';
                        
                        // 昨日漲跌顏色
                        let prevClass = item.prev_pct_chg > 0 ? 'up-price' : (item.prev_pct_chg < 0 ? 'down-price' : '');
                        let prevPrefix = item.prev_pct_chg > 0 ? '+' : '';
                        let displayPrevPct = item.prev_pct_chg !== undefined ? (prevPrefix + item.prev_pct_chg.toFixed(2) + '%') : '--';

                        rows += `<tr class="${rowClass}">
                            <td>${item.code}</td>
                            <td>${item.datetime}</td>
                            <td class="${priceClass}">${item.close}</td>
                            <td class="${priceClass}">${displayPct}</td>
                            <td class="${prevClass}">${displayPrevPct}</td>
                            <td>${item.volume}</td>
                            <td>${item.open}</td>
                            <td>${item.avg_price.toFixed(2)}</td>
                            <td class="text-muted">${item.etf_list || '-'}</td>
                        </tr>`;
                    });
                    $('#data-body').html(rows);
                });
            }
            setInterval(updateData, 1000); // 每秒更新一次
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/data')
def get_data():
    # 將即時資料與 ETF 清單合併
    combined_data = []
    for code, data in tick_store.items():
        temp = data.copy()
        etf_str = stock_etf_map.get(code, "")
        temp['etf_list'] = etf_str
        temp['prev_pct_chg'] = stock_prev_chg_map.get(code, 0.0)
        temp['is_triggered'] = 1 if code in triggered_codes else 0
        # 計算 ETF 數量用於排序
        temp['etf_count'] = len(etf_str.split()) if etf_str else 0
        combined_data.append(temp)
        
    # 排序邏輯：觸發訊號優先，其次是 ETF 數量
    combined_data.sort(key=lambda x: (x['is_triggered'], x['etf_count']), reverse=True)

    return jsonify({
        "ticks": combined_data,
        "us_info": us_market_info
    })

def run_flask():
    # 自動取得電腦在區域網路中的 IP (供同 Wi-Fi 手機使用)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    print(f"\n" + "="*50)
    print(f"🌐 網頁監控介面已啟動！")
    print(f"💻 電腦訪問: http://127.0.0.1:5000")
    print(f"📱 同 Wi-Fi 訪問: http://{local_ip}:5000")
    print(f"🌍 外部訪問: 請啟動 ngrok 並使用其提供的 https 網址")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ==========================================
# 3. 讀取 Excel 並訂閱 (修正版)
# ==========================================
file_path = r"D:\我才不要走量化\etf_換股\前20大etf_unique_list.xlsx"

try:
    print(f"讀取股票清單: {file_path}")
    df = pd.read_excel(file_path)
    
    # 確保代號是乾淨的字串 (去除可能的小數點如 "2330.0")
    # 先轉 float 再轉 int 再轉 str，可以避免 "2330.0" 的情況
    stock_codes = []
    for x in df['stock_code'].astype(str):
        try:
            # 處理成分股代號：若是 ETF (如 919) 則補到 5 位，若是普通股 (如 2330) 則維持 4 位
            s = str(int(float(x)))
            code_str = s.zfill(5) if len(s) <= 3 else s.zfill(4)
        except:
            code_str = str(x)
        stock_codes.append(code_str)
    
    # 【終極補零邏輯】確保每個 ETF 代號都是 5 位數 (如 00919)
    def format_etf_string(s):
        if pd.isna(s) or str(s).strip() == "": return "-"
        # 先把逗號換成空白，再拆分，對每個代號強制補零
        parts = [str(int(float(str(p).strip()))).zfill(5) for p in str(s).replace(',', ' ').split() if str(p).strip()]
        return " ".join(parts)

    # 建立股票與 ETF 的對應 Map
    stock_etf_map = dict(zip(stock_codes, df['etf_list'].apply(format_etf_string)))
    
    # 建立合約列表
    contracts = []
    for code in stock_codes:
        contract = api.Contracts.Stocks[code]
        if contract:
            contracts.append(contract)
        else:
            print(f"警告: 找不到代號 {code} 的合約")

    if contracts:
        # 啟動前先抓取昨日漲跌幅
        fetch_taiwan_stock_prev_chg(contracts)
        
        print(f"開始逐一訂閱 {len(contracts)} 檔股票...")
        
        # 【修正重點】用迴圈一檔一檔訂閱
        for contract in contracts:
            try:
                api.quote.subscribe(
                    contract,
                    quote_type=sj.constant.QuoteType.Tick,
                    version=sj.constant.QuoteVersion.v1,
                    intraday_odd=False # 設為 False 抓取一般整股
                )
                # 為了避免瞬間送出太多請求被擋，可以稍微停頓 (非必要，視情況而定)
                # time.sleep(0.01) 
            except Exception as sub_err:
                print(f"訂閱失敗 {contract.code}: {sub_err}")
        
        # 啟動網頁伺服器執行緒
        print("🌐 啟動網頁監控介面: http://127.0.0.1:5000")
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        print("所有訂閱請求已送出，等待行情推播...")
        
    else:
        print("沒有有效的合約可供訂閱。")

    # ==========================================
    # 4. 保持程式執行
    # ==========================================
    while True:
        now = datetime.now()
        # 策略檢查邏輯：13:20 且 Nasdaq 跌幅 >= 2%
        if now.hour == 13 and now.minute == 20 and not alert_sent:
            if us_market_info["nasdaq_chg"] <= -2.0:
                print("🚨 觸發策略檢查條件 (Nasdaq 跌幅 >= 2%)...")
                triggered_codes.clear() # 清除舊的紀錄
                matched_stocks = []
                for code, data in tick_store.items():
                    if 7.0 <= data["pct_chg"] <= 9.5:
                        matched_stocks.append(data)
                        triggered_codes.add(code) # 紀錄觸發代碼
                
                if matched_stocks:
                    send_strategy_alert(matched_stocks)
                alert_sent = True # 標記今日已檢查
        
        # 每日午夜重置警報開關
        if now.hour == 0 and now.minute == 0:
            alert_sent = False
            
        time.sleep(1)

except KeyboardInterrupt:
    print("\n監控結束，登出 API")
    api.logout()
except Exception as e:
    print(f"發生錯誤: {e}")