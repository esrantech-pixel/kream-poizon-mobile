import io, re, webbrowser, math, json, urllib.request, urllib.parse
from pathlib import Path
import pandas as pd
import streamlit as st
import requests
from PIL import Image, ImageOps, ImageEnhance
import pytesseract

st.set_page_config(page_title='KREAM · POIZON 역소싱 V14 FIELD', layout='wide', initial_sidebar_state='collapsed')

# ---- V13 FIELD: mobile access protection + field layout ----
def _check_app_password():
    try:
        expected = str(st.secrets.get("APP_PASSWORD", "")).strip()
    except Exception:
        expected = ""
    if not expected:
        return True
    if st.session_state.get("_field_auth_ok", False):
        return True
    st.title("🔐 현장 소싱 접속")
    pwd = st.text_input("접속 비밀번호", type="password", key="_field_pwd")
    if st.button("접속", type="primary", width="stretch"):
        if pwd == expected:
            st.session_state["_field_auth_ok"] = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    return False

if not _check_app_password():
    st.stop()

st.markdown("""
<style>
@media (max-width: 768px) {
  .block-container {padding-top: .7rem; padding-left: .6rem; padding-right: .6rem;}
  h1 {font-size: 1.45rem !important;}
  h2, h3 {font-size: 1.12rem !important;}
  div[data-baseweb="tab-list"] {overflow-x:auto; white-space:nowrap;}
  div[data-baseweb="tab"] {min-width:max-content;}
  .stButton > button, .stDownloadButton > button {min-height:46px;}
  input, textarea {font-size:16px !important;}
}
</style>
""", unsafe_allow_html=True)
# ---- end V13 FIELD ----

DATA_DIR = Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / 'products.csv'
KREAM_CACHE_PATH = DATA_DIR / 'kream_latest.csv'
POIZON_CACHE_PATH = DATA_DIR / 'poizon_latest.csv'
DISCOVERY_PATH = DATA_DIR / 'poizon_discovery.csv'

DEFAULT_SETTINGS = {
    'kream_fee_rate': 0.035,
    'poizon_fee_rate': 0.05,
    'shipping_cost': 7000,
    'packing_cost': 1000,
    'target_profit': 15000,
    'target_roi': 20.0,
    'min_30d_sales': 2,
}

if 'settings' not in st.session_state:
    st.session_state.settings = DEFAULT_SETTINGS.copy()

@st.cache_data(show_spinner=False)
def load_db():
    if DB_PATH.exists():
        try:
            d = pd.read_csv(DB_PATH, dtype=str)
            for c in ['model','name','buy_price','memo','kream_model','poizon_model']:
                if c not in d.columns: d[c] = ''
            return d[['model','name','buy_price','memo','kream_model','poizon_model']]
        except Exception:
            pass
    return pd.DataFrame(columns=['model','name','buy_price','memo','kream_model','poizon_model'])

def save_db(df):
    df.to_csv(DB_PATH, index=False, encoding='utf-8-sig')
    # load_db() is cached. Clear immediately so newly saved/updated products
    # are visible to POIZON/KREAM/auto-compare in the same session.
    load_db.clear()


def upsert_product(model, buy_price=None, name=''):
    """Create/update one sourcing product and make it immediately available to compare."""
    model = str(model or '').strip()
    if not model:
        return
    db = load_db().copy()
    hit = db['model'].astype(str).str.strip() == model if len(db) else pd.Series([], dtype=bool)
    bp = '' if buy_price is None else str(int(float(buy_price)))
    if len(db) and hit.any():
        idx = db.index[hit][-1]
        if bp:
            db.loc[idx, 'buy_price'] = bp
        if name and not str(db.loc[idx, 'name']).strip():
            db.loc[idx, 'name'] = str(name).strip()
    else:
        row = {
            'model': model, 'name': str(name or '').strip(), 'buy_price': bp,
            'memo': '', 'kream_model': model, 'poizon_model': model
        }
        db = pd.concat([db, pd.DataFrame([row])], ignore_index=True)
    save_db(db)


def won_to_num(x):
    if x is None or (isinstance(x,float) and pd.isna(x)): return None
    s = str(x).replace(',', '').replace('₩','').replace('원','').strip()
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


def sales_to_num(x):
    if x is None: return None
    s = str(x).strip().replace(',','')
    if not s or s in ['--','-']: return None
    if '<5' in s: return 4
    m = re.search(r'(\d+(?:\.\d+)?)\s*([KkMm])?\+?', s)
    if not m: return None
    n = float(m.group(1)); u = m.group(2)
    if u and u.lower()=='k': n*=1000
    if u and u.lower()=='m': n*=1000000
    return int(n)


def crocs_eu_to_kr(eu_size):
    """Crocs adult range-size mapping used by Baya/Classic style clogs."""
    if eu_size is None:
        return None
    s = str(eu_size).strip().replace('–','-').replace('—','-').replace(' ','')
    mapping = {
        '36-37':'230',
        '37-38':'240',
        '38-39':'250',
        '39-40':'260',
        '41-42':'265',
        '42-43':'270',
        '43-44':'280',
        '45-46':'290',
        '46-47':'300',
        '48-49':'310',
    }
    return mapping.get(s)


def _poizon_block_to_row(block, model='', product_name=''):
    """Parse one POIZON size block copied from seller-center search results."""
    if not block:
        return None

    lines = [re.sub(r'\s+',' ',str(x)).strip() for x in block if str(x).strip()]
    if not lines:
        return None

    joined = '\n'.join(lines)

    # Detect KR size or EU-only size.
    kr = None
    eu = None

    mkr = re.search(
        r'(?:사이즈[:：]?\s*)?KR\s*(\d{3})(?:\s*\(EU\s*([^)]+)\))?',
        joined, re.I
    )
    if mkr:
        kr = mkr.group(1)
        eu = (mkr.group(2) or '').strip() or None

    meu = re.search(
        r'사이즈[:：]?\s*EU\s*([0-9]+(?:[.\u00bd]?[0-9]*)?\s*[-–—]\s*[0-9]+(?:[.\u00bd]?[0-9]*)?)',
        joined, re.I
    )
    if meu:
        eu = meu.group(1).replace('–','-').replace('—','-').replace(' ','')

    # POIZON sometimes copies just "EU 36-37".
    if eu is None:
        meu2 = re.search(
            r'(?<![A-Za-z])EU\s*([0-9]+(?:[.\u00bd]?[0-9]*)?\s*[-–—]\s*[0-9]+(?:[.\u00bd]?[0-9]*)?)',
            joined, re.I
        )
        if meu2:
            eu = meu2.group(1).replace('–','-').replace('—','-').replace(' ','')

    # Brand-specific mapping: Crocs seller center usually exposes EU range sizes.
    pname = str(product_name or '').lower()
    if kr is None and eu and ('crocs' in pname or '크록스' in pname):
        kr = crocs_eu_to_kr(eu)

    # Keep unmapped EU rows visible instead of dropping them.
    # They will not merge with KREAM until a KR size exists.
    if kr is None and eu:
        kr = f'EU {eu}'

    if kr is None:
        return None

    row = {'model': str(model), 'size': str(kr), 'eu_size': eu or ''}

    sku = re.search(r'SKU_ID[:：]?\s*(\d+)', joined, re.I)
    if sku:
        row['sku_id'] = sku.group(1)

    bar = re.search(r'바코드[:：]?\s*([0-9\s]+)', joined, re.I)
    if bar:
        nums = re.findall(r'\d{8,}', bar.group(1))
        if nums:
            row['barcode'] = ' / '.join(nums)

    # Prices in copied seller-center rows normally appear in this order:
    # 1) 최근30일 평균 거래가
    # 2) 중국 구매자 페이지 노출
    # 3) 예상 수익
    won_values = []
    for ln in lines:
        if '₩' in ln or '원' in ln:
            v = won_to_num(ln)
            if v is not None and 1000 <= v <= 10000000:
                won_values.append(v)

    if len(won_values) >= 1:
        row['poizon_avg_price'] = won_values[0]
    if len(won_values) >= 2:
        row['poizon_buyer_price'] = won_values[1]
    if len(won_values) >= 3:
        row['poizon_expected_profit'] = won_values[2]

    # Prefer a sales token after the expected-profit section.
    # Examples: <5, 17, 1.2K
    sales = None
    expected_idx = None
    for i, ln in enumerate(lines):
        if '예상 수익' in ln:
            expected_idx = i
            break

    scan_lines = lines[(expected_idx + 1):] if expected_idx is not None else lines
    # Skip the first won line after "예상 수익" (that's the expected profit itself).
    skipped_profit_value = False
    for ln in scan_lines:
        if ('₩' in ln or '원' in ln) and not skipped_profit_value:
            skipped_profit_value = True
            continue
        token = ln.strip()
        if re.fullmatch(r'<\s*5', token):
            sales = 4
            break
        if re.fullmatch(r'\d+(?:\.\d+)?\s*[KkMm]?\+?', token):
            # avoid long identifiers / barcode-like values
            n = sales_to_num(token)
            if n is not None and n <= 1000000:
                sales = n
                break

    if sales is not None:
        row['poizon_30d_sales'] = sales

    return row


def parse_poizon_paste(text, model='', product_name=''):
    """
    V8 POIZON parser.
    Handles both:
    - old KR-size copy format
    - POIZON seller-center EU-range rows copied with Ctrl+A/Ctrl+C
    """
    raw_lines = [str(x).strip() for x in str(text).splitlines()]
    raw_lines = [x for x in raw_lines if x]

    rows = []

    # Split at every size marker.
    size_starts = []
    for i, ln in enumerate(raw_lines):
        if re.search(r'(?:사이즈[:：]?\s*)?(?:KR\s*\d{3}|EU\s*\d)', ln, re.I):
            size_starts.append(i)

    if size_starts:
        size_starts.append(len(raw_lines))
        for a, b in zip(size_starts[:-1], size_starts[1:]):
            row = _poizon_block_to_row(
                raw_lines[a:b],
                model=model,
                product_name=product_name
            )
            if row:
                rows.append(row)

    # Fallback to old compact KR parser behavior if no split rows were found.
    if not rows:
        lines=[re.sub(r'\s+',' ',x).strip() for x in raw_lines if x.strip()]
        cur={}
        for line in lines:
            m=re.search(r'(?:사이즈[:：]?\s*)?KR\s*(\d{3})(?:\s*\(EU\s*([^)]+)\))?', line, re.I)
            if m:
                if cur.get('size'):
                    rows.append(cur)
                cur={'model':model,'size':m.group(1),'eu_size':m.group(2) or ''}
                continue
            if not cur:
                continue
            if 'SKU_ID' in line:
                mm=re.search(r'SKU_ID[:：]?\s*(\d+)', line)
                if mm:
                    cur['sku_id']=mm.group(1)
            if '바코드' in line:
                nums=re.findall(r'\d{8,}',line)
                if nums:
                    cur['barcode']=' / '.join(nums)
            if '예상 수익' in line:
                v=won_to_num(line)
                if v is not None:
                    cur['poizon_expected_profit']=v
            won_vals=re.findall(r'₩\s*[\d,]+',line)
            if won_vals:
                vals=[won_to_num(v) for v in won_vals]
                if 'poizon_avg_price' not in cur and vals:
                    cur['poizon_avg_price']=vals[0]
                elif 'poizon_buyer_price' not in cur and vals:
                    cur['poizon_buyer_price']=vals[-1]
            if '판매량' in line:
                sm=re.findall(r'(?:<5|\d+[\d,]*(?:\+)?|\d+(?:\.\d+)?[KkMm]\+?)',line)
                if sm:
                    cur['poizon_30d_sales']=sales_to_num(sm[-1])
        if cur.get('size'):
            rows.append(cur)

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)

    # Remove obvious duplicate blocks from Ctrl+A copies.
    dedupe_cols = [c for c in ['model','size','eu_size','sku_id'] if c in out.columns]
    if dedupe_cols:
        out = out.drop_duplicates(subset=dedupe_cols, keep='last')

    # Numeric cleanup.
    for c in ['poizon_avg_price','poizon_buyer_price','poizon_expected_profit','poizon_30d_sales']:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors='coerce')

    return out.reset_index(drop=True)



def parse_kream_full_copy(text, model=''):
    """KREAM 상품/거래내역 화면 전체 복사문에서 실제 체결거래만 추출.

    KREAM 웹 화면은 Ctrl+A → Ctrl+C 시
    W235 / 183,000원 / 26/08/31 처럼 값이 줄바꿈되어 복사될 수 있습니다.
    W/M 접두어와 줄바꿈/탭을 모두 허용해 실제 체결거래만 인식합니다.
    """
    raw = str(text).replace('\u00a0', ' ')
    # 줄바꿈, 탭, 여러 공백을 모두 하나의 공백으로 정규화
    s = re.sub(r'\s+', ' ', raw).strip()

    # 예:
    # W235 183,000원 26/08/31
    # W235\n183,000원\n26/08/31
    # 235(US 6.5) 183,000원 26/08/31
    # 날짜가 붙은 행만 인정하므로 메뉴/배송비/할인가 등은 대부분 배제됩니다.
    pat = re.compile(
        r'(?<![A-Za-z0-9])'
        r'(?:(?P<prefix>[WM])\s*)?'
        r'(?P<size>\d{3})\s*'
        r'(?:\(\s*(?P<label>[^)]{1,20})\s*\))?'
        r'\s+(?P<price>\d{1,3}(?:,\d{3})+|\d{4,7})\s*원?'
        r'\s+(?P<date>\d{2}/\d{2}/\d{2})'
    )

    trades = []
    for m in pat.finditer(s):
        price = won_to_num(m.group('price'))
        if price is None or price < 1000:
            continue
        size = m.group('size')
        prefix = (m.group('prefix') or '').upper()
        label = (m.group('label') or '').strip()
        if prefix and not label:
            label = prefix
        trades.append({
            'model': str(model).strip(),
            'size': size,
            'kream_size_label': label,
            'trade_price': price,
            'trade_date': m.group('date')
        })

    if not trades:
        return pd.DataFrame(), pd.DataFrame()

    detail = pd.DataFrame(trades)
    detail['trade_date_dt'] = pd.to_datetime(detail['trade_date'], format='%y/%m/%d', errors='coerce')
    detail = detail[detail['trade_date_dt'].notna()].copy()
    if detail.empty:
        return pd.DataFrame(), pd.DataFrame()

    # 최신 거래부터 정렬해서 latest_price가 실제 최신 체결가가 되도록 함
    detail = detail.sort_values('trade_date_dt', ascending=False).reset_index(drop=True)

    # 복사된 자료의 가장 최신 거래일을 기준으로 최근 30일 집계
    latest = detail['trade_date_dt'].max()
    cutoff = latest - pd.Timedelta(days=29)
    recent = detail[detail['trade_date_dt'].between(cutoff, latest)].copy()

    # 비교용 가격 = 최근 30일 중앙값 / 판매량 = 최근 30일 체결 건수 / 최신가 = 최신 체결가
    summary = (recent.groupby(['model','size'], as_index=False)
               .agg(kream_price=('trade_price','median'),
                    kream_30d_sales=('trade_price','size'),
                    kream_latest_price=('trade_price','first')))
    summary['kream_price'] = summary['kream_price'].round().astype(float)

    detail = detail.drop(columns=['trade_date_dt'])
    return summary, detail


def normalize_import(df, platform):
    d=df.copy()
    cols={str(c).strip().lower():c for c in d.columns}
    out=pd.DataFrame()
    def pick(names):
        for n in names:
            if n.lower() in cols: return d[cols[n.lower()]]
        return pd.Series([None]*len(d))
    out['model']=pick(['model','상품번호','품번','모델번호','product code','style code'])
    out['size']=pick(['size','사이즈','kr size','kr사이즈'])
    if platform=='POIZON':
        out['poizon_avg_price']=pick(['poizon_avg_price','최근 30일 평균 거래가','평균 거래가','avg price']).map(won_to_num)
        out['poizon_buyer_price']=pick(['poizon_buyer_price','중국 구매자 페이지 노출','구매자 노출가','buyer price']).map(won_to_num)
        out['poizon_30d_sales']=pick(['poizon_30d_sales','최근 30일 판매량','30일 판매량','sales']).map(sales_to_num)
        out['poizon_expected_profit']=pick(['poizon_expected_profit','예상 수익','예상수익']).map(won_to_num)
        out['sku_id']=pick(['sku_id','SKU_ID','sku'])
        out['barcode']=pick(['barcode','바코드'])
    else:
        out['kream_price']=pick(['kream_price','최근 거래가','거래가','판매가','price']).map(won_to_num)
        out['kream_30d_sales']=pick(['kream_30d_sales','최근 30일 판매량','30일 판매량','sales']).map(sales_to_num)
    return out



def upsert_platform_cache(df, path):
    """Parsed platform rows survive Streamlit reruns/redeploys and accumulate by model+size(+SKU)."""
    if df is None or len(df) == 0:
        return
    new = df.copy()
    new['model'] = new['model'].astype(str).str.strip()
    new['size'] = new['size'].astype(str).str.strip()
    try:
        old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame()
    except Exception:
        old = pd.DataFrame()
    all_df = pd.concat([old, new], ignore_index=True) if len(old) else new
    keys = [c for c in ['model','size','sku_id'] if c in all_df.columns]
    if not keys: keys = [c for c in ['model','size'] if c in all_df.columns]
    if keys:
        all_df = all_df.drop_duplicates(subset=keys, keep='last')
    all_df.to_csv(path, index=False, encoding='utf-8-sig')

def load_platform_cache(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()

def canonicalize_platform_models(df, base, platform):
    """Map KREAM/POIZON source model numbers to the registered canonical product code."""
    if df is None or len(df) == 0:
        return df
    d = df.copy()
    d['model'] = d['model'].astype(str).str.strip()
    source_col = 'kream_model' if platform == 'KREAM' else 'poizon_model'
    mapping = {}
    for _, r in base.iterrows():
        canonical = str(r.get('model','')).strip()
        if canonical:
            mapping[canonical] = canonical
        alias = str(r.get(source_col,'') or '').strip()
        if alias and alias.lower() != 'nan':
            mapping[alias] = canonical
    d['source_model'] = d['model']
    d['model'] = d['model'].map(lambda x: mapping.get(x, x))
    return d

def compute_compare(base, kream=None, poizon=None):
    """Compare by exact model+size keys and avoid accidental cross-size cartesian merges."""
    base = base.copy()
    base['model'] = base['model'].astype(str)
    base['buy_price_num'] = base['buy_price'].map(won_to_num)

    frames = []

    # V10.2: 등록 상품별 KREAM/POIZON 별칭을 canonical 모델로 변환합니다.
    kream = canonicalize_platform_models(kream, base, 'KREAM') if kream is not None else kream
    poizon = canonicalize_platform_models(poizon, base, 'POIZON') if poizon is not None else poizon

    # Start from exact canonical-model + KR-size keys observed on either platform.
    key_parts = []
    if kream is not None and len(kream):
        kk = kream.copy()
        kk['model'] = kk['model'].astype(str)
        kk['size'] = kk['size'].astype(str)
        key_parts.append(kk[['model','size']].drop_duplicates())
    if poizon is not None and len(poizon):
        pp = poizon.copy()
        pp['model'] = pp['model'].astype(str)
        pp['size'] = pp['size'].astype(str)
        key_parts.append(pp[['model','size']].drop_duplicates())

    if key_parts:
        keys = pd.concat(key_parts, ignore_index=True).drop_duplicates()
        df = keys.merge(base, on='model', how='left')
    else:
        df = base.copy()
        df['size'] = ''

    # Merge platform data on exact model+size only.
    if kream is not None and len(kream):
        k = kream.copy()
        k['model'] = k['model'].astype(str)
        k['size'] = k['size'].astype(str)
        # one row per model+size
        keep = [c for c in ['model','size','kream_price','kream_30d_sales','kream_latest_price'] if c in k.columns]
        k = k[keep].drop_duplicates(subset=['model','size'])
        df = df.merge(k, on=['model','size'], how='left')

    if poizon is not None and len(poizon):
        p = poizon.copy()
        p['model'] = p['model'].astype(str)
        p['size'] = p['size'].astype(str)

        # Preserve multiple POIZON SKUs for the same KR size by expanding only those exact size rows.
        po_cols = [c for c in [
            'model','size','eu_size','sku_id','barcode',
            'poizon_avg_price','poizon_buyer_price',
            'poizon_30d_sales','poizon_expected_profit'
        ] if c in p.columns]
        p = p[po_cols]

        # Remove the placeholder row for each model+size before merging POIZON variants,
        # then left-expand that exact size only.
        df = df.merge(p, on=['model','size'], how='left')

    s = st.session_state.settings

    if 'kream_price' in df.columns:
        df['kream_net'] = df['kream_price']*(1-s['kream_fee_rate']) - s['shipping_cost'] - s['packing_cost']
        df['kream_profit'] = df['kream_net'] - df['buy_price_num']
        df['kream_roi'] = df['kream_profit']/df['buy_price_num']*100

    if 'poizon_buyer_price' in df.columns:
        payout = df.get('poizon_expected_profit')
        if payout is None:
            payout = df['poizon_buyer_price']*(1-s['poizon_fee_rate'])
        else:
            payout = payout.where(
                payout.notna(),
                df['poizon_buyer_price']*(1-s['poizon_fee_rate'])
            )
        df['poizon_net'] = payout - s['shipping_cost'] - s['packing_cost']
        df['poizon_profit'] = df['poizon_net'] - df['buy_price_num']
        df['poizon_roi'] = df['poizon_profit']/df['buy_price_num']*100

    best = []
    grade = []
    best_profit = []
    best_roi = []
    best_sales = []
    reasons = []
    buy_qty = []

    for _, r in df.iterrows():
        candidates = []

        kp = r.get('kream_profit', None)
        kr = r.get('kream_roi', None)
        ks = r.get('kream_30d_sales', None)
        if pd.notna(kp):
            candidates.append(('KREAM', kp, kr, ks))

        pp = r.get('poizon_profit', None)
        pr = r.get('poizon_roi', None)
        ps = r.get('poizon_30d_sales', None)
        if pd.notna(pp):
            candidates.append(('POIZON', pp, pr, ps))

        if not candidates:
            best.append('데이터부족')
            best_profit.append(None)
            best_roi.append(None)
            best_sales.append(None)
            grade.append('⚪ 데이터부족')
            reasons.append('비교 가능한 판매가 데이터 없음')
            buy_qty.append(0)
            continue

        b = max(candidates, key=lambda x: x[1])
        platform, profit, roi, sales = b
        roi = roi if pd.notna(roi) else None
        sales = sales if pd.notna(sales) else None

        best.append(platform)
        best_profit.append(profit)
        best_roi.append(roi)
        best_sales.append(sales)

        # 손실이면 바로 PASS
        if profit <= 0:
            grade.append('🔴 PASS')
            reasons.append('예상 손실')
            buy_qty.append(0)
            continue

        # POIZON 판매량이 없으면 아무리 수익이 좋아도 관찰 유지
        if platform == 'POIZON' and sales is None:
            grade.append('🟡 관찰')
            fail = []
            if profit < s['target_profit']:
                fail.append(f"순익 {profit:,.0f}원 < 기준 {s['target_profit']:,.0f}원")
            if roi is None:
                fail.append('ROI 데이터 없음')
            elif roi < s['target_roi']:
                fail.append(f"ROI {roi:.1f}% < 기준 {s['target_roi']:.1f}%")
            fail.append('POIZON 30일 판매량 데이터 없음')
            reasons.append(' / '.join(fail))
            buy_qty.append(0)
            continue

        # 수익/ROI 기준 미달이면 관찰
        fail = []
        if profit < s['target_profit']:
            fail.append(f"순익 {profit:,.0f}원 < 기준 {s['target_profit']:,.0f}원")
        if roi is None:
            fail.append('ROI 데이터 없음')
        elif roi < s['target_roi']:
            fail.append(f"ROI {roi:.1f}% < 기준 {s['target_roi']:.1f}%")

        if fail:
            grade.append('🟡 관찰')
            reasons.append(' / '.join(fail))
            buy_qty.append(0)
            continue

        # 여기부터 수익/ROI 기준은 통과
        min_sales = max(int(s['min_30d_sales']), 1)
        sales_num = int(sales) if sales is not None else 0

        # 강력매입: 마진이 크고 회전도 확인된 경우
        strong_profit = profit >= s['target_profit'] * 1.5
        strong_roi = roi is not None and roi >= max(s['target_roi'] + 20, 40)
        strong_sales = sales_num >= max(min_sales + 1, 3)

        if strong_profit and strong_roi and strong_sales:
            grade.append('🟢🟢 강력매입')
            reasons.append('고수익·고ROI·회전 모두 충족')
            # 최근 판매량의 절반 수준으로 보수적 재고 제안, 최대 5개
            qty = max(2, min(5, int(math.ceil(sales_num / 2))))
            buy_qty.append(qty)
        elif sales_num >= min_sales:
            grade.append('🟢 매입추천')
            reasons.append('수익·ROI·회전 기준 충족')
            qty = 2 if sales_num >= 4 else 1
            buy_qty.append(qty)
        elif sales_num >= 1:
            grade.append('🟠 1개 테스트')
            reasons.append(f'수익성은 충족하나 최근 30일 판매 {sales_num}건으로 회전 낮음')
            buy_qty.append(1)
        else:
            grade.append('🟡 관찰')
            reasons.append('수익성은 충족하나 판매량 데이터 부족')
            buy_qty.append(0)

    df['best_platform'] = best
    df['best_profit'] = best_profit
    df['best_roi'] = best_roi
    df['best_30d_sales'] = best_sales
    df['판정'] = grade
    df['판정이유'] = reasons
    df['추천구매수량'] = buy_qty

    # Maximum purchase price that still satisfies current target profit + ROI.
    max_buy = []
    guidance = []
    for _, r in df.iterrows():
        platform = r.get('best_platform')
        mb = None
        if platform == 'KREAM' and pd.notna(r.get('kream_price', None)):
            mb = calc_max_buy_price(
                r.get('kream_price'),
                s['kream_fee_rate'],
                s['shipping_cost'],
                s['packing_cost'],
                s['target_profit'],
                s['target_roi']
            )
        elif platform == 'POIZON' and pd.notna(r.get('poizon_buyer_price', None)):
            # When POIZON expected-profit/payout exists we keep the existing profit engine,
            # but max-buy-price uses buyer-visible price and the configured estimated fee.
            mb = calc_max_buy_price(
                r.get('poizon_buyer_price'),
                s['poizon_fee_rate'],
                s['shipping_cost'],
                s['packing_cost'],
                s['target_profit'],
                s['target_roi']
            )
        max_buy.append(mb)

        cur = r.get('buy_price_num')
        sales = r.get('best_30d_sales')
        if mb is None or pd.isna(mb):
            guidance.append('가격 데이터 확인')
        elif cur is not None and pd.notna(cur) and cur > mb:
            guidance.append(f'현재 매입가가 권장 상한보다 {cur-mb:,.0f}원 높음')
        elif sales is None or pd.isna(sales):
            guidance.append('수익성은 확인됨 · 판매량 확인 필요')
        elif r.get('추천구매수량', 0) == 1:
            guidance.append('1개 테스트 권장')
        elif r.get('추천구매수량', 0) >= 2:
            guidance.append(f"추천 {int(r.get('추천구매수량', 0))}개 · 분할매입")
        elif sales is None or pd.isna(sales):
            guidance.append('수익성은 확인됨 · 판매량 확인 필요')
        else:
            guidance.append('관찰 유지')

    df['권장최대매입가'] = max_buy
    df['매입가이드'] = guidance
    return df


def load_discovery_db():
    cols = [
        'model','name','kr_size','eu_size','poizon_sku',
        'poizon_30d_sales','poizon_avg_price','poizon_buyer_price',
        'korea_source','korea_buy_price','memo'
    ]
    if DISCOVERY_PATH.exists():
        try:
            d = pd.read_csv(DISCOVERY_PATH, dtype=str)
            for c in cols:
                if c not in d.columns:
                    d[c] = ''
            return d[cols]
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def save_discovery_db(df):
    d = df.copy()
    d.to_csv(DISCOVERY_PATH, index=False, encoding='utf-8-sig')


def score_discovery(df):
    """V11 역소싱 후보 판정: POIZON 회전 → 국내 매입가 → 수익성 순으로 판단."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    d = df.copy()
    for c in ['model','name','kr_size','eu_size','poizon_sku','korea_source','memo']:
        if c not in d.columns:
            d[c] = ''
    d['poizon_30d_sales_num'] = d.get('poizon_30d_sales', '').map(sales_to_num)
    d['poizon_avg_price_num'] = d.get('poizon_avg_price', '').map(won_to_num)
    d['poizon_buyer_price_num'] = d.get('poizon_buyer_price', '').map(won_to_num)
    d['korea_buy_price_num'] = d.get('korea_buy_price', '').map(won_to_num)

    stg, reason, priority = [], [], []
    exp_profit, exp_roi, max_buy = [], [], []
    s = st.session_state.settings

    for _, r in d.iterrows():
        sales = r.get('poizon_30d_sales_num')
        buyer = r.get('poizon_buyer_price_num')
        avgp = r.get('poizon_avg_price_num')
        buy = r.get('korea_buy_price_num')
        sell_ref = buyer if pd.notna(buyer) else avgp

        # 1) 한국 판매자 30일 판매량을 가장 먼저 본다.
        if sales is None or pd.isna(sales):
            base_stage = '⚪ 판매량 확인'
            base_reason = 'POIZON 한국 판매자 30일 판매량 입력 필요'
            pr = 5
        elif sales >= 100:
            base_stage = '🔥 최우선 조사'
            base_reason = f'한국 판매자 30일 {int(sales)}개'
            pr = 1
        elif sales >= 50:
            base_stage = '🟢 좋은 후보'
            base_reason = f'한국 판매자 30일 {int(sales)}개'
            pr = 2
        elif sales >= 20:
            base_stage = '🟡 후보'
            base_reason = f'한국 판매자 30일 {int(sales)}개'
            pr = 3
        else:
            base_stage = '⚪ 관찰'
            base_reason = f'한국 판매자 30일 {int(sales) if pd.notna(sales) else 0}개 < 20개'
            pr = 4

        profit = roi = mb = None
        final_stage = base_stage
        final_reason = base_reason

        if sell_ref is not None and pd.notna(sell_ref):
            mb = calc_max_buy_price(
                sell_ref, s['poizon_fee_rate'], s['shipping_cost'], s['packing_cost'],
                s['target_profit'], s['target_roi']
            )

        # 국내 소싱가까지 입력되면 실제 1족 테스트 여부를 판정한다.
        if buy is not None and pd.notna(buy) and buy > 0 and sell_ref is not None and pd.notna(sell_ref):
            net = float(sell_ref) * (1 - float(s['poizon_fee_rate'])) - float(s['shipping_cost']) - float(s['packing_cost'])
            profit = net - float(buy)
            roi = profit / float(buy) * 100 if buy else None
            if profit <= 0:
                final_stage = '🔴 PASS'
                final_reason = f'예상 손실 {profit:,.0f}원'
                pr = 9
            elif profit >= s['target_profit'] and roi is not None and roi >= s['target_roi'] and sales is not None and pd.notna(sales) and sales >= 20:
                final_stage = '🟠 1족 테스트'
                final_reason = f'30일 {int(sales)}개 · 예상순익 {profit:,.0f}원 · ROI {roi:.1f}%'
                pr = 0
            else:
                fails=[]
                if profit < s['target_profit']:
                    fails.append(f'순익 {profit:,.0f}원 < {s["target_profit"]:,.0f}원')
                if roi is None or roi < s['target_roi']:
                    fails.append(f'ROI {roi:.1f}% < {s["target_roi"]:.1f}%' if roi is not None else 'ROI 확인 필요')
                if sales is None or pd.isna(sales) or sales < 20:
                    fails.append('30일 판매량 20개 미만')
                final_stage = '🟡 관찰'
                final_reason = ' / '.join(fails) if fails else base_reason
                pr = 4

        stg.append(final_stage); reason.append(final_reason); priority.append(pr)
        exp_profit.append(profit); exp_roi.append(roi); max_buy.append(mb)

    d['판정'] = stg
    d['판정이유'] = reason
    d['예상순익'] = exp_profit
    d['예상ROI'] = exp_roi
    d['권장최대매입가'] = max_buy
    d['_priority'] = priority
    d = d.sort_values(['_priority','poizon_30d_sales_num','예상순익'], ascending=[True,False,False], na_position='last')
    return d.drop(columns=['_priority'])





def _prepare_ocr_images(uploaded):
    """V14: return several OCR-friendly variants for price tags / box labels."""
    if uploaded is None:
        return []
    try:
        uploaded.seek(0)
    except Exception:
        pass
    img = Image.open(uploaded).convert("RGB")
    max_side = 2400
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    gray = ImageOps.grayscale(img)
    contrast = ImageEnhance.Contrast(gray).enhance(2.2)
    sharp = ImageEnhance.Sharpness(contrast).enhance(2.0)
    # Hard threshold often helps small box-label/model-code text.
    threshold = sharp.point(lambda x: 255 if x > 165 else 0)
    return [gray, sharp, threshold]


def _ocr_text(uploaded):
    """V14: multi-pass OCR. Returns (best_text, diagnostic_message)."""
    if uploaded is None:
        return "", "사진 없음"
    variants = _prepare_ocr_images(uploaded)
    if not variants:
        return "", "이미지를 열 수 없음"

    attempts = []
    errors = []
    # PSM 6: block, PSM 11: sparse label text.
    for img in variants:
        for lang in ("kor+eng", "eng"):
            for psm in (6, 11):
                try:
                    txt = pytesseract.image_to_string(img, lang=lang, config=f"--oem 3 --psm {psm}")
                    txt = str(txt or '').strip()
                    if txt:
                        # Reward useful retail patterns rather than random OCR noise.
                        score = len(txt)
                        score += 80 * len(re.findall(r"\b[A-Z]{1,4}\d{3,6}(?:-\d{2,4})?\b", txt.upper()))
                        score += 35 * len(re.findall(r"(?:₩|KRW|원|\d{1,3}[,\s]\d{3})", txt, re.I))
                        score += 25 * len(re.findall(r"\b(?:NIKE|ADIDAS|CROCS|ASICS|PUMA|SALOMON|NEPA)\b", txt.upper()))
                        attempts.append((score, txt, lang, psm))
                except Exception as e:
                    errors.append(str(e))

    if not attempts:
        msg = errors[-1] if errors else "글자를 찾지 못했습니다"
        return "", msg[:300]
    attempts.sort(key=lambda x: x[0], reverse=True)
    _, best, lang, psm = attempts[0]
    return best, f"인식 성공 · {lang} · PSM {psm}"


def _extract_product_fields(ocr_text):
    """V14 conservative extraction: avoid filling fields from weak/random OCR."""
    raw = str(ocr_text or "")
    up = raw.upper()

    # Model/style code: require a mixed letter+digit code of useful length.
    candidates = re.findall(r"(?<![A-Z0-9])[A-Z]{1,4}[\s-]?\d{3,6}(?:-\d{2,4})?(?![A-Z0-9])", up)
    model = ""
    for c in candidates:
        c = re.sub(r"\s+", "", c)
        if 5 <= len(c) <= 14 and re.search(r"[A-Z]", c) and re.search(r"\d", c):
            model = c
            break

    # Price: only accept stronger price forms (comma/원/KRW/₩), reducing random numbers.
    nums = []
    price_patterns = [
        r"(?:₩|KRW)\s*([0-9]{1,3}(?:[,\s][0-9]{3})+)",
        r"([0-9]{1,3}(?:[,\s][0-9]{3})+)\s*원",
        r"(?:SALE|PRICE|판매가|할인가|정상가)[^\d]{0,12}([0-9]{4,7})",
    ]
    for pat in price_patterns:
        for m in re.findall(pat, raw, re.I):
            v = re.sub(r"[^0-9]", "", str(m))
            if v:
                n = int(v)
                if 10000 <= n <= 5000000:
                    nums.append(n)
    nums = sorted(set(nums))
    buy_price = nums[0] if nums else 0
    retail_price = nums[-1] if len(nums) >= 2 else (nums[0] if nums else 0)

    discount = ""
    dm = re.search(r"(?<!\d)(\d{1,2})\s*%", raw)
    if dm and 1 <= int(dm.group(1)) <= 90:
        discount = dm.group(1)

    sizes = []
    # Prefer explicitly labelled CM/KR sizes.
    for m in re.findall(r"(?:CM|KR|SIZE|사이즈)\s*[:：]?\s*(2[2-9](?:\.5)?|3[0-2](?:\.5)?|2[2-9]\d|3[0-2]\d)", up):
        try:
            n = float(m)
            mm = int(round(n * 10)) if n < 100 else int(n)
            if 220 <= mm <= 325 and str(mm) not in sizes:
                sizes.append(str(mm))
        except Exception:
            pass

    brand = ""
    brand_map = [
        ("NEW BALANCE", ["NEW BALANCE", "NEWBALANCE"]),
        ("THE NORTH FACE", ["THE NORTH FACE", "NORTH FACE"]),
        ("NIKE", ["NIKE"]), ("ADIDAS", ["ADIDAS"]), ("PUMA", ["PUMA"]),
        ("CROCS", ["CROCS"]), ("ASICS", ["ASICS"]), ("SALOMON", ["SALOMON"]), ("NEPA", ["NEPA"]),
    ]
    for canonical, aliases in brand_map:
        if any(a in up for a in aliases):
            brand = canonical
            break

    lines = [re.sub(r"\s+", " ", x).strip() for x in raw.splitlines() if x.strip()]
    name = ""
    keys = ["AIR MAX","AIR FORCE","DUNK","JORDAN","SAMBA","GAZELLE","ADIZERO","ULTRABOOST","CLIFTON","GEL-","XT-","2002R"]
    for ln in lines:
        if any(k in ln.upper() for k in keys):
            name = ln[:100]
            break

    return {"brand":brand,"model":model,"name":name,"buy_price":buy_price,
            "retail_price":retail_price,"discount":discount,"sizes":", ".join(sizes[:12])}

def send_telegram_message(text):
    """Send a Telegram message using Streamlit secrets via requests."""
    try:
        token = str(st.secrets.get('TELEGRAM_BOT_TOKEN', '')).strip()
        chat_id = str(st.secrets.get('TELEGRAM_CHAT_ID', '')).strip()
    except Exception:
        token = chat_id = ''
    if not token or not chat_id:
        return False, '텔레그램 설정이 없습니다. .streamlit/secrets.toml에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 넣어주세요.'
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        resp = requests.post(url, data={'chat_id': chat_id, 'text': text}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return bool(data.get('ok')), ('전송 완료' if data.get('ok') else str(data))
    except requests.exceptions.SSLError as e:
        return False, f'SSL 전송 실패: {e}'
    except requests.exceptions.RequestException as e:
        return False, f'네트워크 전송 실패: {e}'
    except Exception as e:
        return False, f'전송 실패: {e}'

def candidate_telegram_text(df, title='⭐ KREAM·POIZON 소싱 후보'):
    if df is None or len(df) == 0:
        return title + '\n현재 전송할 후보가 없습니다.'
    lines=[title, f'후보 {len(df)}개']
    for i, (_, r) in enumerate(df.head(20).iterrows(), 1):
        def val(name, default='-'):
            x=r.get(name, default)
            return default if pd.isna(x) else x
        def won(name):
            try: return f"{float(val(name,0)):,.0f}원"
            except: return '-'
        try: roi=f"{float(val('최고ROI(%)',0)):.1f}%"
        except: roi='-'
        lines += [
            '', f"{i}. {val('판정','')} {val('상품명','')} {val('모델','')}",
            f"사이즈: KR {val('KR사이즈','-')} / EU {val('EU사이즈','-')}",
            f"매입가: {won('현재매입가')} / 최대매입가: {won('권장최대매입가')}",
            f"판매처: {val('추천판매처','-')} / 예상순익: {won('최고예상순익')} / ROI: {roi}",
            f"30일 판매: {val('추천처30일판매','-')} / 추천수량: {val('추천구매수량',0)}개"
        ]
    if len(df)>20: lines += ['', f'외 {len(df)-20}개는 프로그램 후보목록에서 확인']
    return '\n'.join(lines)

def calc_max_buy_price(sell_price, fee_rate, shipping_cost, packing_cost, target_profit, target_roi):
    """Return the maximum purchase price satisfying BOTH target profit and target ROI."""
    if sell_price is None or pd.isna(sell_price):
        return None
    net_before_buy = float(sell_price) * (1 - float(fee_rate)) - float(shipping_cost) - float(packing_cost)
    # Profit constraint: net_before_buy - buy >= target_profit
    by_profit = net_before_buy - float(target_profit)
    # ROI constraint: (net_before_buy - buy)/buy >= target_roi
    roi = float(target_roi) / 100.0
    by_roi = net_before_buy / (1 + roi) if roi > -1 else None
    vals = [v for v in [by_profit, by_roi] if v is not None]
    if not vals:
        return None
    ans = min(vals)
    return max(ans, 0.0)


st.title('KREAM · POIZON 역소싱 V14 FIELD')
st.caption('POIZON에서 먼저 잘 팔리는 상품을 찾고 → 한국에서 싸게 소싱한 뒤 → KREAM/POIZON 수익성과 회전율을 비교하는 역소싱 도구입니다.')

with st.sidebar:
    st.header('판정 기준')
    s=st.session_state.settings
    s['shipping_cost']=st.number_input('건당 배송비(원)',0,100000,int(s['shipping_cost']),500)
    s['packing_cost']=st.number_input('포장/기타비(원)',0,50000,int(s['packing_cost']),500)
    s['kream_fee_rate']=st.number_input('KREAM 수수료율',0.0,0.5,float(s['kream_fee_rate']),0.005,format='%.3f')
    s['poizon_fee_rate']=st.number_input('POIZON 추정 수수료율',0.0,0.5,float(s['poizon_fee_rate']),0.005,format='%.3f')
    s['target_profit']=st.number_input('추천 최소 순익',0,200000,int(s['target_profit']),1000)
    s['target_roi']=st.number_input('추천 최소 ROI(%)',0.0,200.0,float(s['target_roi']),1.0)
    s['min_30d_sales']=st.number_input('추천 최소 30일 판매량',0,100000,int(s['min_30d_sales']),10)
    st.info('수수료는 실제 계정/카테고리에 따라 달라질 수 있습니다. 판매 확정 전 플랫폼 정산화면으로 최종 확인하세요.')

tf,t0,t1,t2,t3,t4,t5,t6=st.tabs(['📸 현장 카메라','🔥 POIZON 후보발굴','① 상품등록','② POIZON 가져오기','③ KREAM 가져오기','④ 자동 비교','⑤ 사용법','⑥ 오늘 살 것'])


with tf:
    st.subheader("📸 현장 카메라 판정 V14")
    st.caption("가격표/상품 사진과 박스 라벨을 찍으면 품번·가격·사이즈를 1차 자동 인식합니다. 인식값은 반드시 확인·수정 후 저장하세요.")
    st.info("현장 권장: ① 가격표/상품 사진 1장 + ② 박스 라벨 1장. 같은 상품을 두 장 찍으면 인식률이 좋아집니다.")

    c1, c2 = st.columns(2)
    with c1:
        photo_price = st.camera_input("① 상품/가격표 촬영", key="field_photo_price")
        upload_price = st.file_uploader("또는 가격표 사진 선택", type=["jpg","jpeg","png"], key="field_upload_price")
    with c2:
        photo_label = st.camera_input("② 박스 라벨 촬영", key="field_photo_label")
        upload_label = st.file_uploader("또는 라벨 사진 선택", type=["jpg","jpeg","png"], key="field_upload_label")

    img1 = photo_price or upload_price
    img2 = photo_label or upload_label

    if st.button("🤖 사진 자동 인식", type="primary", width="stretch", key="field_ocr_button"):
        if img1 is None and img2 is None:
            st.warning("먼저 가격표 또는 박스 라벨 사진을 1장 이상 넣어주세요.")
        else:
            with st.spinner("사진 글자를 읽는 중입니다..."):
                text1, status1 = _ocr_text(img1)
                text2, status2 = _ocr_text(img2)
                combined = (text1 + "\n" + text2).strip()
                st.session_state["field_ocr_raw"] = combined
                st.session_state["field_ocr_status"] = f"① {status1} / ② {status2}"
                f = _extract_product_fields(combined)
                for k, v in f.items():
                    st.session_state[f"field_{k}"] = v
            if combined:
                if f.get("model") or f.get("buy_price") or f.get("brand"):
                    st.success("사진 인식 완료. 아래 값이 맞는지 확인해 주세요.")
                else:
                    st.warning("글자는 읽었지만 품번/가격을 확실하게 찾지 못했습니다. 가격표와 박스 라벨을 가까이서 다시 찍어주세요.")
            else:
                st.error("사진에서 글자를 읽지 못했습니다. 아래 OCR 상태를 확인해 주세요.")

    ocr_status = st.session_state.get("field_ocr_status", "")
    if ocr_status:
        st.caption("OCR 상태: " + ocr_status)
    raw_ocr = st.session_state.get("field_ocr_raw", "")
    if raw_ocr:
        with st.expander("OCR 원문 확인"):
            st.text(raw_ocr)

    st.markdown("### 자동 인식 결과 — 틀리면 바로 수정")
    fc1, fc2 = st.columns(2)
    brand_v = fc1.text_input("브랜드", key="field_brand")
    model_v = fc2.text_input("모델/품번", key="field_model")
    name_v = st.text_input("상품명", key="field_name")

    pc1, pc2, pc3 = st.columns(3)
    buy_v = pc1.number_input("현장 매입가(원)", min_value=0, max_value=10000000, step=500, key="field_buy_price")
    retail_v = pc2.number_input("정상가(원)", min_value=0, max_value=10000000, step=500, key="field_retail_price")
    disc_v = pc3.text_input("할인율(%)", key="field_discount")

    sizes_v = st.text_input("확인된 KR 사이즈", placeholder="예: 260, 265", key="field_sizes")

    s1, s2 = st.columns(2)
    if s1.button("💾 이 상품 저장", width="stretch", key="field_save_product"):
        if not str(model_v).strip():
            st.error("모델/품번을 확인해 주세요.")
        elif int(buy_v or 0) <= 0:
            st.error("실제 매입가를 확인해 주세요.")
        else:
            display_name = (str(brand_v).strip() + " " + str(name_v).strip()).strip()
            upsert_product(str(model_v).strip(), int(buy_v), display_name)
            st.session_state["pmodel"] = str(model_v).strip()
            st.success(f"저장 완료: {model_v} / {int(buy_v):,}원")

    q_model = str(model_v or "").strip()
    if q_model:
        st.markdown("### 현장 빠른 조회")
        q1, q2, q3 = st.columns(3)
        q1.link_button("POIZON 검색", f"https://kr.poizon.com/search?keyword={q_model}", width="stretch")
        q2.link_button("KREAM 검색", f"https://kream.co.kr/search?keyword={q_model}", width="stretch")
        q3.link_button("무신사 검색", f"https://www.musinsa.com/search/goods?keyword={q_model}", width="stretch")
        st.caption("현재 V13은 사진 자동입력까지 구현했습니다. POIZON 완전자동 가격조회는 Open Platform API 키 연결 후 다음 단계에서 붙입니다.")

    if s2.button("📲 촬영정보 텔레그램 전송", width="stretch", key="field_send_telegram"):
        if not q_model:
            st.error("먼저 모델/품번을 확인해 주세요.")
        else:
            msg = (
                "📸 [현장 소싱 촬영]\n"
                f"브랜드: {brand_v or '-'}\n"
                f"상품명: {name_v or '-'}\n"
                f"모델/품번: {q_model}\n"
                f"매입가: {int(buy_v or 0):,}원\n"
                f"정상가: {int(retail_v or 0):,}원\n"
                f"할인율: {disc_v or '-'}%\n"
                f"사이즈: {sizes_v or '-'}\n"
                "→ POIZON/KREAM 가격 확인 후 최종 매입판정"
            )
            ok, m = send_telegram_message(msg)
            (st.success if ok else st.error)(m)


with t0:
    st.subheader('🔥 POIZON 역소싱 후보발굴 V11.0')
    st.caption('처음에는 상품을 사지 않습니다. POIZON 한국 판매자 30일 판매량 → 잘 팔리는 사이즈 → 국내 소싱가 순서로 확인합니다.')

    st.markdown('### 1) POIZON에서 먼저 후보 찾기')
    st.info('초보 기준: 한국 판매자 30일 판매량 100개↑ 최우선 조사 · 50개↑ 좋은 후보 · 20개↑ 후보 · 20개 미만 관찰')

    discovery = load_discovery_db()
    discovery_edit = st.data_editor(
        discovery,
        num_rows='dynamic',
        width='stretch',
        key='discovery_editor_v11',
        column_config={
            'model':'모델/품번',
            'name':'상품명',
            'kr_size':'KR사이즈',
            'eu_size':'EU사이즈',
            'poizon_sku':'POIZON SKU',
            'poizon_30d_sales':'한국판매자 30일판매',
            'poizon_avg_price':'POIZON 30일평균가',
            'poizon_buyer_price':'중국 구매자 노출가',
            'korea_source':'국내 소싱처',
            'korea_buy_price':'국내 매입가',
            'memo':'메모'
        }
    )

    csave, cclear = st.columns([3,1])
    if csave.button('💾 후보발굴 목록 저장', width='stretch', key='save_discovery_v11'):
        save_discovery_db(discovery_edit)
        st.success('POIZON 후보발굴 목록을 저장했습니다.')
    if cclear.button('🧹 후보발굴 목록 비우기', width='stretch', key='clear_discovery_v11'):
        save_discovery_db(pd.DataFrame(columns=discovery.columns))
        st.rerun()

    ranked = score_discovery(discovery_edit)
    if len(ranked):
        st.markdown('### 2) 자동 우선순위')
        show_cols=[c for c in [
            '판정','판정이유','model','name','kr_size','eu_size','poizon_sku',
            'poizon_30d_sales_num','poizon_avg_price_num','poizon_buyer_price_num',
            'korea_source','korea_buy_price_num','권장최대매입가','예상순익','예상ROI','memo'
        ] if c in ranked.columns]
        show=ranked[show_cols].rename(columns={
            'model':'모델','name':'상품명','kr_size':'KR사이즈','eu_size':'EU사이즈','poizon_sku':'POIZON SKU',
            'poizon_30d_sales_num':'한국판매자30일판매','poizon_avg_price_num':'POIZON평균가',
            'poizon_buyer_price_num':'POIZON구매자노출가','korea_source':'국내소싱처',
            'korea_buy_price_num':'국내매입가','memo':'메모','예상ROI':'예상ROI(%)'
        })
        st.dataframe(
            show,
            width='stretch',
            height=460,
            column_config={
                'POIZON평균가': st.column_config.NumberColumn(format='%,.0f원'),
                'POIZON구매자노출가': st.column_config.NumberColumn(format='%,.0f원'),
                '국내매입가': st.column_config.NumberColumn(format='%,.0f원'),
                '권장최대매입가': st.column_config.NumberColumn(format='%,.0f원'),
                '예상순익': st.column_config.NumberColumn(format='%,.0f원'),
                '예상ROI(%)': st.column_config.NumberColumn(format='%.1f%%'),
            }
        )

        testable = ranked[ranked['판정']=='🟠 1족 테스트']
        if len(testable):
            st.success(f'지금 1족 테스트 가능한 후보가 {len(testable)}개 있습니다. 실제 결제 직전 가격·재고·검수 가능 여부를 다시 확인하세요.')
        else:
            st.warning('아직 바로 살 상품은 없습니다. 판매량 높은 후보부터 국내 최저가를 찾아 입력하세요.')

        st.download_button(
            '📥 POIZON 후보발굴 랭킹 CSV 저장',
            ranked.to_csv(index=False).encode('utf-8-sig'),
            'poizon_reverse_sourcing_v11.csv','text/csv',width='stretch'
        )

    st.markdown('### 3) 빠른 역소싱 검색')
    q = st.text_input('확인할 모델/품번', placeholder='예: IE3677, DD1391-100', key='reverse_q_v11')
    if q:
        c1,c2,c3=st.columns(3)
        c1.link_button('POIZON에서 검색',f'https://kr.poizon.com/search?keyword={q}',width='stretch')
        c2.link_button('KREAM에서 검색',f'https://kream.co.kr/search?keyword={q}',width='stretch')
        c3.link_button('무신사에서 검색',f'https://www.musinsa.com/search/goods?keyword={q}',width='stretch')
        st.caption('순서: POIZON 한국판매량 확인 → 잘 팔리는 사이즈 기록 → 국내 소싱가 확인 → 1족 테스트 여부 판단')

with t1:
    st.subheader('소싱 후보 등록')
    db=load_db()
    edited=st.data_editor(db, num_rows='dynamic', width='stretch', column_config={
        'model':'모델/품번','name':'상품명','buy_price':'실제 매입가(원)','memo':'메모','kream_model':'KREAM 모델명','poizon_model':'POIZON 모델명'
    })
    c1,c2=st.columns(2)
    if c1.button('💾 후보 저장',width='stretch'):
        save_db(edited); st.success('저장했습니다.')
    if c2.button('🧹 비우기',width='stretch'):
        save_db(pd.DataFrame(columns=db.columns)); st.rerun()
    st.markdown('**빠른 검색**')
    model=st.text_input('모델/품번',placeholder='예: IE3677')
    if model:
        c1,c2=st.columns(2)
        c1.link_button('KREAM에서 검색',f'https://kream.co.kr/search?keyword={model}',width='stretch')
        c2.link_button('POIZON에서 검색',f'https://kr.poizon.com/search?keyword={model}',width='stretch')

with t2:
    st.subheader('POIZON 데이터')
    st.write('가장 편한 방법 하나만 쓰면 됩니다: **파일 업로드** 또는 **판매자센터 표를 복사해서 붙여넣기**.')
    poizon_df=pd.DataFrame()
    up=st.file_uploader('POIZON CSV/XLSX 업로드',type=['csv','xlsx','xls'],key='pz')
    if up:
        try:
            raw=pd.read_excel(up) if up.name.lower().endswith(('xlsx','xls')) else pd.read_csv(up)
            poizon_df=normalize_import(raw,'POIZON')
            st.dataframe(poizon_df,width='stretch')
        except Exception as e: st.error(f'파일을 읽지 못했습니다: {e}')
    st.markdown('**판매자센터에서 상품을 펼친 뒤 Ctrl+A → Ctrl+C → 아래에 Ctrl+V 하세요. V8은 EU 범위 사이즈 행도 자동 인식합니다.**')
    _base_for_poizon = load_db()
    _latest_model = str(_base_for_poizon.iloc[-1]['model']) if len(_base_for_poizon) else 'IE3677'
    if 'pmodel' not in st.session_state:
        st.session_state['pmodel'] = _latest_model
    pmodel=st.text_input('이 붙여넣기의 모델/품번',key='pmodel')

    # Keep the actual sourcing price with the current model. This avoids the old
    # 'data shortage' result caused by a missing/stale product DB row.
    _existing_buy = 0
    _db_for_buy = load_db()
    if len(_db_for_buy) and str(pmodel).strip():
        _bhit = _db_for_buy[_db_for_buy['model'].astype(str).str.strip() == str(pmodel).strip()]
        if len(_bhit):
            _existing_buy = int(won_to_num(_bhit.iloc[-1].get('buy_price')) or 0)
    _buy_key = f'current_buy_price__{str(pmodel).strip()}'
    if _buy_key not in st.session_state:
        st.session_state[_buy_key] = _existing_buy
    current_buy_price = st.number_input(
        '이번 상품 실제 매입가(원)', min_value=0, max_value=10000000,
        step=1000, key=_buy_key,
        help='한 번 입력하면 상품 DB에 저장되어 KREAM/POIZON 자동 비교까지 유지됩니다.'
    )

    _pname = ''
    if len(_base_for_poizon):
        _hit = _base_for_poizon[_base_for_poizon['model'].astype(str) == str(pmodel)]
        if len(_hit):
            _pname = str(_hit.iloc[-1].get('name',''))

    if _pname:
        st.caption(f'현재 상품: {_pname}')
    if ('crocs' in _pname.lower()) or ('크록스' in _pname.lower()):
        st.info('Crocs 상품은 POIZON의 EU 범위 사이즈(예: EU 36-37)를 KR mm로 자동 변환합니다.')

    pasted=st.text_area('POIZON 표 붙여넣기',height=260,key='ppaste')
    if pasted:
        parsed=parse_poizon_paste(pasted,pmodel,_pname)
        if len(parsed):
            poizon_df=parsed
            mapped = parsed[~parsed['size'].astype(str).str.startswith('EU ')].shape[0] if 'size' in parsed.columns else 0
            st.success(f'{len(parsed)}개 POIZON 사이즈 행을 인식했습니다. KR 매칭 가능 {mapped}개')
            st.dataframe(parsed,width='stretch')
            if 'size' in parsed.columns and parsed['size'].astype(str).str.startswith('EU ').any():
                st.warning('일부 EU 사이즈는 KR mm로 자동 변환하지 못했습니다. 이 행은 KREAM과 자동 매칭되지 않습니다.')
        else:
            st.warning('POIZON 사이즈 행을 인식하지 못했습니다. 상품을 펼친 상태에서 Ctrl+A → Ctrl+C 후 다시 붙여넣어 주세요.')
    if len(poizon_df):
        # Persist BOTH platform data and sourcing cost before leaving this tab.
        upsert_product(pmodel, current_buy_price if current_buy_price > 0 else None, _pname)
        st.session_state['poizon_df']=poizon_df.copy()
        upsert_platform_cache(poizon_df, POIZON_CACHE_PATH)
        st.download_button('POIZON 정리본 CSV 저장',poizon_df.to_csv(index=False).encode('utf-8-sig'),'poizon_clean.csv','text/csv')
    elif str(pmodel).strip() and current_buy_price > 0:
        # Save cost immediately even before POIZON parsing succeeds.
        upsert_product(pmodel, current_buy_price, _pname)

with t3:
    st.subheader('KREAM 데이터')
    st.write('KREAM 상품의 **거래 및 입찰 내역**을 열고, 화면에서 Ctrl+A → Ctrl+C 한 뒤 아래에 그대로 붙여넣으세요. 메뉴 글자는 자동으로 버리고 실제 체결거래만 추출합니다.')
    kream_df=pd.DataFrame()
    upk=st.file_uploader('KREAM CSV/XLSX 업로드',type=['csv','xlsx','xls'],key='kr')
    if upk:
        try:
            raw=pd.read_excel(upk) if upk.name.lower().endswith(('xlsx','xls')) else pd.read_csv(upk)
            kream_df=normalize_import(raw,'KREAM')
            st.dataframe(kream_df,width='stretch')
        except Exception as e: st.error(f'파일을 읽지 못했습니다: {e}')

    st.markdown('**KREAM 화면 전체 복사 붙여넣기**')
    # V10.3: POIZON에서 작업 중인 canonical 모델을 기준으로 등록된 KREAM 모델명을 자동 연결
    _base_for_kream = load_db()
    _canonical_for_kream = str(st.session_state.get('pmodel', '')).strip()
    _kream_default = ''
    _kream_product_name = ''
    if len(_base_for_kream) and _canonical_for_kream:
        _hit = _base_for_kream[_base_for_kream['model'].astype(str).str.strip() == _canonical_for_kream]
        if len(_hit) == 0:
            _hit = _base_for_kream[_base_for_kream['poizon_model'].astype(str).str.strip() == _canonical_for_kream]
        if len(_hit):
            _row = _hit.iloc[-1]
            _canonical_for_kream = str(_row.get('model','')).strip()
            _alias = str(_row.get('kream_model','') or '').strip()
            _kream_default = _alias if _alias and _alias.lower() != 'nan' else _canonical_for_kream
            _kream_product_name = str(_row.get('name','')).strip()

    # 등록 DB에 없는 새 품번이라도 이전 상품(Crocs 등)을 끌고 오지 않고
    # 현재 POIZON에서 작업 중인 모델/품번 자체를 기본값으로 사용
    if not _kream_default:
        _kream_default = _canonical_for_kream or ''

    # 대상 상품이 바뀌었을 때만 입력값을 새 KREAM 모델명으로 갱신
    if st.session_state.get('_kream_target_canonical') != _canonical_for_kream:
        st.session_state['kmodel'] = _kream_default
        st.session_state['_kream_target_canonical'] = _canonical_for_kream
    elif 'kmodel' not in st.session_state:
        st.session_state['kmodel'] = _kream_default

    km=st.text_input('모델/품번',key='kmodel')
    if st.session_state.get('_last_kream_model_for_paste') != km:
        st.session_state['_last_kream_model_for_paste'] = km
    if _kream_product_name:
        st.caption(f'현재 상품: {_kream_product_name} · KREAM 모델명: {_kream_default}')
    ktext=st.text_area('KREAM 거래 및 입찰 내역에서 Ctrl+A → Ctrl+C → 여기에 Ctrl+V',height=260,key='kpaste')

    if ktext:
        parsed_k, detail_k = parse_kream_full_copy(ktext, km)
        if len(parsed_k):
            kream_df=parsed_k
            total_trades=int(parsed_k['kream_30d_sales'].sum())
            st.success(f'실제 체결거래를 인식했습니다. 최근 30일 {total_trades}건 / {len(parsed_k)}개 사이즈')
            st.dataframe(parsed_k,width='stretch')
            with st.expander('인식한 체결거래 상세 보기'):
                st.dataframe(detail_k,width='stretch',height=420)
            st.download_button('KREAM 정리본 CSV 저장',
                               parsed_k.to_csv(index=False).encode('utf-8-sig'),
                               'kream_clean.csv','text/csv')
        else:
            st.warning('체결거래를 찾지 못했습니다. KREAM의 「거래 및 입찰 내역」 창에서 거래 목록이 보이는 상태로 Ctrl+A → Ctrl+C 해서 다시 붙여넣어 주세요.')

    if len(kream_df):
        st.session_state['kream_df']=kream_df.copy()
        upsert_platform_cache(kream_df, KREAM_CACHE_PATH)

with t4:
    st.subheader('실전 소싱 판정 V13')
    # Always read the newest product cost table after tab changes.
    load_db.clear()
    base=load_db()
    if len(base)==0:
        st.warning('① 상품등록 탭에서 모델/품번과 매입가를 먼저 등록해 주세요.')
    else:
        p=st.session_state.get('poizon_df',pd.DataFrame())
        k=st.session_state.get('kream_df',pd.DataFrame())
        # 세션이 비어도 이전에 정상 인식해 저장한 플랫폼 자료를 자동 복원
        if p is None or len(p) == 0: p = load_platform_cache(POIZON_CACHE_PATH)
        if k is None or len(k) == 0: k = load_platform_cache(KREAM_CACHE_PATH)

        # Helpful status: lets us immediately see whether all three sources survived tab changes.
        _p_models = set(p['model'].astype(str).str.strip()) if p is not None and len(p) and 'model' in p.columns else set()
        _k_models = set(k['model'].astype(str).str.strip()) if k is not None and len(k) and 'model' in k.columns else set()
        _b_models = set(base['model'].astype(str).str.strip()) if len(base) else set()
        st.caption(f'연결 상태 · 상품DB {len(base)}행 / POIZON {0 if p is None else len(p)}행 / KREAM {0 if k is None else len(k)}행')
        _active_models = (_p_models | _k_models)
        _missing_cost = sorted([m for m in _active_models if m and m not in _b_models])
        if _missing_cost:
            st.warning('매입가가 저장되지 않은 모델: ' + ', '.join(_missing_cost) + ' · ② POIZON 가져오기에서 실제 매입가를 입력해 주세요.')

        result=compute_compare(base,k,p)

        rank_map={'🟢🟢 강력매입':0,'🟢 매입추천':1,'🟠 1개 테스트':2,'🟡 관찰':3,'🔴 PASS':4,'⚪ 데이터부족':5}
        result['_rank']=result['판정'].map(rank_map).fillna(9)
        result=result.sort_values(['_rank','best_profit'],ascending=[True,False],na_position='last').drop(columns=['_rank'])

        strong_n=int((result['판정']=='🟢🟢 강력매입').sum())
        rec_n=int((result['판정']=='🟢 매입추천').sum())
        test_n=int((result['판정']=='🟠 1개 테스트').sum())
        watch_n=int((result['판정']=='🟡 관찰').sum())
        pass_n=int((result['판정']=='🔴 PASS').sum())

        c1,c2,c3,c4,c5=st.columns(5)
        c1.metric('🟢🟢 강력매입',strong_n)
        c2.metric('🟢 매입추천',rec_n)
        c3.metric('🟠 1개 테스트',test_n)
        c4.metric('🟡 관찰',watch_n)
        c5.metric('🔴 PASS',pass_n)

        if strong_n or rec_n or test_n:
            st.success(
                f'실전 후보: 강력매입 {strong_n}개 · 매입추천 {rec_n}개 · 1개 테스트 {test_n}개'
            )
        else:
            st.warning('현재 기준에서는 바로 살 후보가 없습니다. 관찰 후보의 이유와 권장 최대 매입가를 확인해 보세요.')

        compact_cols=[
            '판정','판정이유','매입가이드','추천구매수량','model','size','eu_size','sku_id',
            'buy_price_num','권장최대매입가',
            'best_platform','best_profit','best_roi','best_30d_sales',
            'kream_price','kream_30d_sales',
            'poizon_avg_price','poizon_buyer_price','poizon_30d_sales'
        ]
        compact_cols=[c for c in compact_cols if c in result.columns]
        compact=result[compact_cols].copy()

        rename_map={
            '판정':'판정',
            '판정이유':'이유',
            '매입가이드':'매입가이드',
            '추천구매수량':'추천구매수량',
            'model':'모델',
            'size':'KR사이즈',
            'eu_size':'EU사이즈',
            'sku_id':'POIZON SKU',
            'buy_price_num':'현재매입가',
            '권장최대매입가':'권장최대매입가',
            'best_platform':'추천판매처',
            'best_profit':'최고예상순익',
            'best_roi':'최고ROI(%)',
            'best_30d_sales':'추천처30일판매',
            'kream_price':'KREAM가격',
            'kream_30d_sales':'KREAM30일판매',
            'poizon_avg_price':'POIZON평균가',
            'poizon_buyer_price':'POIZON구매자노출가',
            'poizon_30d_sales':'POIZON30일판매'
        }
        compact=compact.rename(columns=rename_map)

        st.markdown('### 한눈에 보기')
        st.dataframe(
            compact,
            width='stretch',
            height=560,
            column_config={
                '현재매입가': st.column_config.NumberColumn(format='%,.0f원'),
                '권장최대매입가': st.column_config.NumberColumn(format='%,.0f원'),
                '최고예상순익': st.column_config.NumberColumn(format='%,.0f원'),
                '최고ROI(%)': st.column_config.NumberColumn(format='%.1f%%'),
                'KREAM가격': st.column_config.NumberColumn(format='%,.0f원'),
                'POIZON평균가': st.column_config.NumberColumn(format='%,.0f원'),
                'POIZON구매자노출가': st.column_config.NumberColumn(format='%,.0f원'),
            }
        )

        st.markdown('### 실전 매입 해석')
        actionable = compact[compact['판정'].isin(['🟢🟢 강력매입','🟢 매입추천','🟠 1개 테스트'])] if '판정' in compact.columns else pd.DataFrame()
        if len(actionable):
            for _, r in actionable.head(12).iterrows():
                qty = int(r.get('추천구매수량',0)) if pd.notna(r.get('추천구매수량',0)) else 0
                st.success(
                    f"{r.get('판정','')} | {r.get('모델','')} / KR {r.get('KR사이즈','')} → "
                    f"{r.get('추천판매처','')} | 예상순익 {r.get('최고예상순익',0):,.0f}원 | "
                    f"ROI {r.get('최고ROI(%)',0):.1f}% | 권장 최대 매입가 "
                    f"{(r.get('권장최대매입가') if pd.notna(r.get('권장최대매입가')) else 0):,.0f}원 | "
                    f"추천 구매수량 {qty}개"
                )
        else:
            st.info('현재 기준에서 강력매입/매입추천/1개 테스트 후보가 없습니다.')

        st.info('POIZON은 30일 판매량 데이터가 없으면 수익성이 좋아도 자동으로 🟡 관찰로 유지합니다. 실제 매입 전 POIZON 판매량/회전성은 별도로 확인하세요.')


        # ===== V6: 실전 소싱 후보 누적 저장 / 자동 랭킹 =====
        candidate_path = DATA_DIR / 'sourcing_candidates.csv'

        st.markdown('### ⭐ 소싱 후보목록')

        actionable_rows = compact[
            compact['판정'].isin([
                '🟢🟢 강력매입',
                '🟢 매입추천',
                '🟠 1개 테스트'
            ])
        ].copy() if '판정' in compact.columns else pd.DataFrame()

        csave, cclear = st.columns([3, 1])

        with csave:
            if st.button('⭐ 이번 분석 후보를 후보목록에 저장', width='stretch'):
                if len(actionable_rows) == 0:
                    st.warning('현재 저장할 강력매입/매입추천/1개 테스트 후보가 없습니다.')
                else:
                    save_rows = actionable_rows.copy()
                    save_rows['저장일시'] = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
                    # 후보목록에서 보기 쉽도록 상품명도 함께 저장
                    if '상품명' not in save_rows.columns:
                        name_map = dict(zip(base['model'].astype(str), base['name'].astype(str))) if 'name' in base.columns else {}
                        save_rows['상품명'] = save_rows['모델'].astype(str).map(name_map).fillna('')

                    if candidate_path.exists():
                        try:
                            old_candidates = pd.read_csv(candidate_path, dtype=str)
                        except Exception:
                            old_candidates = pd.DataFrame()
                    else:
                        old_candidates = pd.DataFrame()

                    if len(old_candidates):
                        all_candidates = pd.concat(
                            [old_candidates, save_rows.astype(str)],
                            ignore_index=True
                        )
                    else:
                        all_candidates = save_rows.astype(str)

                    key_cols = [
                        c for c in ['모델', 'KR사이즈', 'EU사이즈', 'POIZON SKU']
                        if c in all_candidates.columns
                    ]
                    if key_cols:
                        all_candidates = all_candidates.drop_duplicates(
                            subset=key_cols,
                            keep='last'
                        )

                    all_candidates.to_csv(
                        candidate_path,
                        index=False,
                        encoding='utf-8-sig'
                    )
                    st.success(f'후보 {len(save_rows)}개를 저장했습니다.')

        with cclear:
            if candidate_path.exists():
                if st.button('🗑 후보목록 비우기', width='stretch'):
                    try:
                        candidate_path.unlink()
                        st.success('후보목록을 비웠습니다.')
                        st.rerun()
                    except Exception as e:
                        st.warning(f'후보목록 삭제 실패: {e}')

        if candidate_path.exists():
            try:
                saved = pd.read_csv(candidate_path)

                if len(saved):
                    rank_order = {
                        '🟢🟢 강력매입': 1,
                        '🟢 매입추천': 2,
                        '🟠 1개 테스트': 3
                    }
                    saved['_등급순위'] = saved['판정'].map(rank_order).fillna(9)

                    if '최고예상순익' in saved.columns:
                        saved['_순익정렬'] = pd.to_numeric(
                            saved['최고예상순익'],
                            errors='coerce'
                        ).fillna(-1)
                    else:
                        saved['_순익정렬'] = 0

                    if '최고ROI(%)' in saved.columns:
                        saved['_ROI정렬'] = pd.to_numeric(
                            saved['최고ROI(%)'],
                            errors='coerce'
                        ).fillna(-999)
                    else:
                        saved['_ROI정렬'] = 0

                    saved = saved.sort_values(
                        ['_등급순위', '_순익정렬', '_ROI정렬'],
                        ascending=[True, False, False]
                    ).drop(columns=['_등급순위', '_순익정렬', '_ROI정렬'])

                    st.caption(f'현재 저장된 실전 후보: {len(saved)}개')
                    st.dataframe(saved, width='stretch', height=360)

                    if st.button('📡 텔레그램 연결 테스트', width='stretch', key='telegram_connection_test_v114'):
                        ok, msg = send_telegram_message('✅ KREAM·POIZON V11.4 텔레그램 연결 성공')
                        (st.success if ok else st.error)(msg)
                    if st.button('📲 저장 후보를 텔레그램으로 전송', width='stretch', key='telegram_saved_candidates_v113'):
                        ok, msg = send_telegram_message(candidate_telegram_text(saved))
                        (st.success if ok else st.error)(msg)

                    st.download_button(
                        '📥 전체 소싱 후보 CSV 저장',
                        saved.to_csv(index=False).encode('utf-8-sig'),
                        'sourcing_candidates.csv',
                        'text/csv',
                        width='stretch'
                    )
                else:
                    st.info('아직 저장된 후보가 없습니다.')

            except Exception as e:
                st.warning(f'후보목록을 읽지 못했습니다: {e}')
        else:
            st.info('아직 저장된 후보가 없습니다.')

        st.download_button(
            '📥 실전 판정표 CSV 저장',
            compact.to_csv(index=False).encode('utf-8-sig'),
            'kream_poizon_decision_v5.csv',
            'text/csv',
            width='stretch'
        )

        with st.expander('상세 계산 데이터 보기'):
            order=[
                'model','name','size','eu_size','sku_id','buy_price_num','권장최대매입가','추천구매수량','매입가이드',
                'kream_price','kream_30d_sales','kream_latest_price','kream_profit','kream_roi',
                'poizon_avg_price','poizon_buyer_price','poizon_30d_sales','poizon_expected_profit','poizon_profit','poizon_roi',
                'best_platform','best_profit','best_roi','best_30d_sales','판정','판정이유'
            ]
            show=[c for c in order if c in result.columns]
            st.dataframe(result[show],width='stretch',height=520)

        st.caption('※ 같은 모델+같은 KR 사이즈끼리만 비교합니다. POIZON에서 같은 KR 사이즈에 여러 EU/SKU가 있으면 SKU별로 별도 행으로 표시합니다.')

with t5:
    st.subheader('V11.0 가장 쉬운 사용 순서')
    st.markdown('''
1. **🔥 POIZON 후보발굴**에서 한국 판매자 30일 판매량이 높은 상품을 먼저 기록합니다. **100개↑ 최우선 / 50개↑ 좋은 후보 / 20개↑ 후보**로 봅니다.  
2. 후보 상품의 **잘 팔리는 사이즈**와 최근 가격을 확인한 뒤, 국내에서 같은 모델/SKU를 가장 싸게 살 수 있는 곳을 찾습니다.  
3. 국내 매입가까지 넣으면 V11이 **예상 순익·ROI·권장 최대 매입가**를 계산해 `🟠 1족 테스트` 여부를 판정합니다.  
4. 실제로 살 후보만 **① 상품등록**에 옮기고, `② POIZON 가져오기`와 `③ KREAM 가져오기`에서 상세 사이즈 데이터를 붙여넣습니다.  
5. `④ 자동 비교`에서 최종 판정을 확인하고, 처음에는 **잘 팔리는 사이즈 1족만 테스트 매입**합니다.  
6. `⑥ 오늘 살 것`에서는 저장된 실전 후보를 예산 안에서 구매 우선순위로 정리합니다.

**V11의 목적은 '싸 보이는 물건을 먼저 사는 것'이 아니라, POIZON에서 실제로 팔리는 상품을 먼저 찾고 한국에서 싸게 소싱하는 것입니다.**  
완전 자동 로그인/수집은 플랫폼의 공식 API 또는 허용된 연동 방식이 확인된 뒤 붙이는 것이 안전합니다.
''')

with t6:
    st.subheader('오늘 살 것 V11.0')
    st.caption('저장한 실전 후보를 예산 안에서 구매 우선순위로 정리합니다. 실제 매입 직전에는 플랫폼 가격과 재고를 한 번 더 확인하세요.')

    candidate_path = DATA_DIR / 'sourcing_candidates.csv'

    if not candidate_path.exists():
        st.info('아직 저장된 후보가 없습니다. ④ 자동 비교에서 강력매입/매입추천/1개 테스트 후보를 먼저 저장해 주세요.')
    else:
        try:
            saved = pd.read_csv(candidate_path)

            if saved.empty:
                st.info('아직 저장된 후보가 없습니다.')
            else:
                numeric_cols = [
                    '추천구매수량','현재매입가','권장최대매입가',
                    '최고예상순익','최고ROI(%)','추천처30일판매'
                ]
                for c in numeric_cols:
                    if c in saved.columns:
                        saved[c] = pd.to_numeric(saved[c], errors='coerce')

                # 최신 후보만 유지
                key_cols = [c for c in ['모델','KR사이즈','EU사이즈','POIZON SKU'] if c in saved.columns]
                if key_cols and '저장일시' in saved.columns:
                    saved = saved.sort_values('저장일시').drop_duplicates(subset=key_cols, keep='last')

                rank_order = {
                    '🟢🟢 강력매입': 1,
                    '🟢 매입추천': 2,
                    '🟠 1개 테스트': 3,
                    '🟡 관찰': 4
                }
                saved['_등급순위'] = saved['판정'].map(rank_order).fillna(9)
                saved['_순익정렬'] = pd.to_numeric(saved.get('최고예상순익'), errors='coerce').fillna(-1)
                saved['_ROI정렬'] = pd.to_numeric(saved.get('최고ROI(%)'), errors='coerce').fillna(-999)
                saved['_판매정렬'] = pd.to_numeric(saved.get('추천처30일판매'), errors='coerce').fillna(0)

                saved = saved.sort_values(
                    ['_등급순위','_순익정렬','_ROI정렬','_판매정렬'],
                    ascending=[True,False,False,False]
                ).drop(columns=['_등급순위','_순익정렬','_ROI정렬','_판매정렬'])

                if '추천구매수량' not in saved.columns:
                    saved['추천구매수량'] = 0
                saved['추천구매수량'] = pd.to_numeric(saved['추천구매수량'], errors='coerce').fillna(0).astype(int)

                # ---------------------------
                # 오늘 예산 / 필터
                # ---------------------------
                st.markdown('### 오늘 장보기 조건')
                c_budget, c_roi, c_profit = st.columns(3)
                with c_budget:
                    today_budget = st.number_input('오늘 매입 예산(원)', min_value=0, value=300000, step=50000, key='today_budget_v10')
                with c_roi:
                    today_min_roi = st.number_input('오늘 최소 ROI(%)', min_value=-100.0, value=float(st.session_state.settings['target_roi']), step=1.0, key='today_min_roi_v10')
                with c_profit:
                    today_min_profit = st.number_input('오늘 최소 순익(원)', min_value=-100000, value=int(st.session_state.settings['target_profit']), step=1000, key='today_min_profit_v10')

                working = saved.copy()
                working['현재매입가'] = pd.to_numeric(working.get('현재매입가'), errors='coerce').fillna(0)
                working['최고예상순익'] = pd.to_numeric(working.get('최고예상순익'), errors='coerce').fillna(0)
                working['최고ROI(%)'] = pd.to_numeric(working.get('최고ROI(%)'), errors='coerce').fillna(-999)
                working['추천처30일판매'] = pd.to_numeric(working.get('추천처30일판매'), errors='coerce').fillna(0)

                # 실제 구매 후보만 남김
                working = working[
                    working['판정'].isin(['🟢🟢 강력매입','🟢 매입추천','🟠 1개 테스트']) &
                    (working['최고ROI(%)'] >= today_min_roi) &
                    (working['최고예상순익'] >= today_min_profit) &
                    (working['현재매입가'] > 0) &
                    (working['추천구매수량'] > 0)
                ].copy()

                # 예산 안에서 상위 후보부터 추천 수량을 채움
                remaining = int(today_budget)
                budget_qty = []
                for _, r in working.iterrows():
                    unit = int(r['현재매입가']) if pd.notna(r['현재매입가']) else 0
                    want = int(r['추천구매수량']) if pd.notna(r['추천구매수량']) else 0
                    if unit <= 0 or want <= 0 or remaining < unit:
                        q = 0
                    else:
                        q = min(want, remaining // unit)
                    budget_qty.append(int(q))
                    remaining -= int(q) * unit

                working['오늘구매수량'] = budget_qty
                working = working[working['오늘구매수량'] > 0].copy()
                working['예상매입금액'] = working['현재매입가'] * working['오늘구매수량']
                working['예상총순익'] = working['최고예상순익'] * working['오늘구매수량']
                working.insert(0, '우선순위', range(1, len(working)+1))

                total_cost = int(working['예상매입금액'].sum()) if len(working) else 0
                total_profit = int(working['예상총순익'].sum()) if len(working) else 0
                total_qty = int(working['오늘구매수량'].sum()) if len(working) else 0
                remain_budget = max(int(today_budget) - total_cost, 0)
                blended_roi = (total_profit / total_cost * 100) if total_cost > 0 else 0

                c1,c2,c3,c4 = st.columns(4)
                c1.metric('오늘 구매수량', f'{total_qty}개')
                c2.metric('예상 총 매입금액', f'{total_cost:,.0f}원')
                c3.metric('예상 총 순익', f'{total_profit:,.0f}원')
                c4.metric('예산 잔액', f'{remain_budget:,.0f}원')

                if total_cost > 0:
                    st.success(f'예산 기준 예상 종합 ROI {blended_roi:.1f}% · 아래 순서대로 매장에서 재고를 확인하세요.')
                else:
                    st.warning('현재 저장 후보 중 오늘 기준을 충족하는 구매 대상이 없습니다.')

                st.markdown('### 🛒 실전 장보기 리스트')
                show_cols = [c for c in [
                    '우선순위','판정','상품명','모델','KR사이즈','EU사이즈',
                    '오늘구매수량','현재매입가','예상매입금액',
                    '추천판매처','최고예상순익','예상총순익',
                    '최고ROI(%)','추천처30일판매','권장최대매입가','매입가이드','저장일시'
                ] if c in working.columns]

                st.dataframe(
                    working[show_cols] if len(working) else pd.DataFrame(columns=show_cols),
                    width='stretch',
                    height=500,
                    column_config={
                        '현재매입가': st.column_config.NumberColumn(format='%,.0f원'),
                        '예상매입금액': st.column_config.NumberColumn(format='%,.0f원'),
                        '최고예상순익': st.column_config.NumberColumn(format='%,.0f원'),
                        '예상총순익': st.column_config.NumberColumn(format='%,.0f원'),
                        '권장최대매입가': st.column_config.NumberColumn(format='%,.0f원'),
                        '최고ROI(%)': st.column_config.NumberColumn(format='%.1f%%'),
                    }
                )

                # 판매처별 묶음
                if len(working) and '추천판매처' in working.columns:
                    st.markdown('### 판매처별 요약')
                    by_platform = working.groupby('추천판매처', dropna=False).agg(
                        상품종류=('모델','count'),
                        구매수량=('오늘구매수량','sum'),
                        예상매입금액=('예상매입금액','sum'),
                        예상총순익=('예상총순익','sum')
                    ).reset_index()
                    st.dataframe(
                        by_platform,
                        width='stretch',
                        column_config={
                            '예상매입금액': st.column_config.NumberColumn(format='%,.0f원'),
                            '예상총순익': st.column_config.NumberColumn(format='%,.0f원'),
                        }
                    )

                if st.button('📲 오늘 장보기 리스트를 텔레그램으로 전송', width='stretch', key='telegram_today_v113'):
                    ok, msg = send_telegram_message(candidate_telegram_text(working, '🛒 오늘 장보기 리스트'))
                    (st.success if ok else st.error)(msg)

                st.download_button(
                    '📥 오늘 장보기 리스트 CSV 저장',
                    (working[show_cols] if len(working) else pd.DataFrame(columns=show_cols)).to_csv(index=False).encode('utf-8-sig'),
                    'today_buy_list_v11.csv',
                    'text/csv',
                    width='stretch'
                )

                st.info('후보 저장 후 가격이 변할 수 있습니다. 실제 결제 직전 KREAM/POIZON 가격과 매장 재고를 다시 확인하세요.')

        except Exception as e:
            st.error(f'후보목록을 읽지 못했습니다: {e}')

