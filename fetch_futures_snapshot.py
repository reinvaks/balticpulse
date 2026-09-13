from __future__ import annotations
import json, re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import pandas as pd
import requests

OUT = Path("data/futures.json")
UA = {"User-Agent":"Mozilla/5.0 (compatible; BalticPulseFutures/1.0)", "Accept-Language":"en-US,en;q=0.9"}

EURONEXT = {
    "SYS_M": ("NSBM", "Nordic SYS", "month"),
    "SYS_Q": ("NSBQ", "Nordic SYS", "quarter"),
    "SYS_Y": ("NSBY", "Nordic SYS", "year"),
    "FI_M": ("HLBM", "FI EPAD", "month"),
    "FI_Q": ("HLBQ", "FI EPAD", "quarter"),
    "FI_Y": ("HLBY", "FI EPAD", "year"),
    "LT_M": ("VIBM", "LT EPAD", "month"),
    "LT_Q": ("VIBQ", "LT EPAD", "quarter"),
    "LT_Y": ("VIBY", "LT EPAD", "year"),
}
ICE_URL = "https://www.ice.com/products/27996665/Dutch-TTF-Natural-Gas-Futures/data"

MONTHS={m:i for i,m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],1)}

def num(v):
    if v is None: return None
    s=str(v).replace('\xa0',' ').strip()
    if s in ('','-','—','nan'): return None
    s=s.replace(',','')
    try: return float(s)
    except: return None

def delivery_start(label):
    s=str(label).strip()
    m=re.fullmatch(r'([A-Z][a-z]{2})\s+(20\d{2})',s)
    if m and m.group(1) in MONTHS: return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}-01"
    m=re.fullmatch(r'Q([1-4])\s+(20\d{2})',s,re.I)
    if m: return f"{m.group(2)}-{(int(m.group(1))-1)*3+1:02d}-01"
    m=re.fullmatch(r'(20\d{2})',s)
    if m: return f"{m.group(1)}-01-01"
    m=re.fullmatch(r'([A-Z][a-z]{2})(\d{2})',s)
    if m and m.group(1) in MONTHS: return f"20{m.group(2)}-{MONTHS[m.group(1)]:02d}-01"
    m=re.fullmatch(r'Q([1-4])\s*(\d{2})',s,re.I)
    if m: return f"20{m.group(2)}-{(int(m.group(1))-1)*3+1:02d}-01"
    m=re.fullmatch(r'Cal\s*(\d{2})',s,re.I)
    if m: return f"20{m.group(1)}-01-01"
    m=re.fullmatch(r'(Winter|Summer)(\d{2})',s,re.I)
    if m:
        yr=2000+int(m.group(2)); mo=10 if m.group(1).lower()=='winter' else 4
        return f"{yr}-{mo:02d}-01"
    return None

def norm_cols(df):
    df=df.copy(); df.columns=[str(c).strip().replace('\n',' ') for c in df.columns]
    return df

def find_price_table(tables):
    for df in tables:
        d=norm_cols(df)
        cols=[c.lower() for c in d.columns]
        if any('delivery' in c or 'contract' in c for c in cols) and any('settl' in c or c=='last' or 'last' in c for c in cols):
            return d
    return None

def pick_col(df, needles):
    for c in df.columns:
        lc=str(c).lower()
        if any(n in lc for n in needles): return c
    return None

def fetch_euronext(code, series, tenor):
    url=f"https://live.euronext.com/en/product/commodities-futures/{code}-DAMS"
    r=requests.get(url,timeout=(5,25),headers=UA); r.raise_for_status()
    tables=pd.read_html(StringIO(r.text))
    df=find_price_table(tables)
    if df is None: raise ValueError(f"{code}: prices table not found")
    dcol=pick_col(df,['delivery','maturity']) or df.columns[0]
    scol=pick_col(df,['settl'])
    oicol=pick_col(df,['o.i','open interest'])
    if scol is None: raise ValueError(f"{code}: settlement column not found: {list(df.columns)}")
    rows=[]
    for _,row in df.iterrows():
        delivery=str(row[dcol]).strip(); sett=num(row[scol])
        ds=delivery_start(delivery)
        if sett is None or not ds: continue
        rows.append({'series':series,'tenor':tenor,'delivery':delivery,'delivery_start':ds,'settlement':sett,
                     'open_interest':num(row[oicol]) if oicol else None,'source_product':code,'source_url':url})
    if not rows: raise ValueError(f"{code}: no valid price rows")
    return rows

def implied(sys_rows, epad_rows, name):
    idx={(r['tenor'],r['delivery']):r for r in epad_rows}
    out=[]
    for s in sys_rows:
        e=idx.get((s['tenor'],s['delivery']))
        if not e: continue
        out.append({**s,'series':name,'settlement':s['settlement']+e['settlement'],
                    'open_interest':None,'source_product':f"{s['source_product']}+{e['source_product']}",
                    'source_url':'https://live.euronext.com/en/products/commodities/commodity-futures/list'})
    return out

def select_key_power(curve):
    rows=[]
    labels=[('M+1','month'),('Q+1','quarter'),('Y+1','year')]
    for series in ['Nordic SYS','FI implied','LT implied']:
        sub=[r for r in curve if r['series']==series]
        for label,tenor in labels:
            cand=sorted([r for r in sub if r['tenor']==tenor],key=lambda x:x['delivery_start'])
            if cand:
                r=cand[0].copy(); rows.append({'series':series,'horizon':label,'delivery':r['delivery'],
                    'settlement':r['settlement'],'open_interest':r.get('open_interest'),'source_product':r['source_product']})
    return rows

def fetch_ice():
    r=requests.get(ICE_URL,timeout=(5,25),headers=UA); r.raise_for_status()
    tables=pd.read_html(StringIO(r.text)); df=find_price_table(tables)
    if df is None: raise ValueError('ICE TTF: data table not found')
    ccol=pick_col(df,['contract']) or df.columns[0]; lastcol=pick_col(df,['last']); timecol=pick_col(df,['time']); volcol=pick_col(df,['volume'])
    if lastcol is None: raise ValueError(f'ICE TTF: last column not found: {list(df.columns)}')
    rows=[]
    for _,row in df.iterrows():
        contract=str(row[ccol]).strip(); last=num(row[lastcol]); ds=delivery_start(contract)
        if last is None or not ds: continue
        rows.append({'contract':contract,'delivery_start':ds,'last':last,
                     'time':str(row[timecol]) if timecol else '', 'volume':num(row[volcol]) if volcol else None,
                     'source_url':ICE_URL})
    if not rows: raise ValueError('ICE TTF: no valid rows')
    return rows

def select_key_gas(curve):
    def typ(c):
        s=c['contract']
        if re.fullmatch(r'[A-Z][a-z]{2}\d{2}',s): return 'M+1'
        if re.fullmatch(r'Q[1-4]\s*\d{2}',s,re.I): return 'Q+1'
        if re.fullmatch(r'Cal\s*\d{2}',s,re.I): return 'Y+1'
        return None
    out=[]; seen=set()
    for r in sorted(curve,key=lambda x:x['delivery_start']):
        t=typ(r)
        if t and t not in seen:
            out.append({'horizon':t,'contract':r['contract'],'last':r['last'],'volume':r.get('volume'),'time':r.get('time')}); seen.add(t)
    return out

def main():
    previous={}
    if OUT.exists():
        try: previous=json.loads(OUT.read_text(encoding='utf-8'))
        except: previous={}
    errors=[]; power_curve=[]
    buckets={}
    for key,(code,series,tenor) in EURONEXT.items():
        try: buckets[key]=fetch_euronext(code,series,tenor)
        except Exception as e: errors.append(f"Euronext {code}: {type(e).__name__}: {e}")
    sys=sum([buckets.get(k,[]) for k in ['SYS_M','SYS_Q','SYS_Y']],[])
    fi=sum([buckets.get(k,[]) for k in ['FI_M','FI_Q','FI_Y']],[])
    lt=sum([buckets.get(k,[]) for k in ['LT_M','LT_Q','LT_Y']],[])
    power_curve=sys+implied(sys,fi,'FI implied')+implied(sys,lt,'LT implied')
    power={'curve':power_curve,'key':select_key_power(power_curve),'errors':errors.copy()}
    gas_errors=[]
    try:
        gas_curve=fetch_ice(); gas={'curve':gas_curve,'key':select_key_gas(gas_curve),'errors':gas_errors}
    except Exception as e:
        gas_errors.append(f"ICE TTF: {type(e).__name__}: {e}"); gas={'curve':[],'key':[],'errors':gas_errors}
    if not power['curve'] and previous.get('power',{}).get('curve'): power=previous['power']; power.setdefault('errors',[]).extend(errors)
    if not gas['curve'] and previous.get('gas',{}).get('curve'): gas=previous['gas']; gas.setdefault('errors',[]).extend(gas_errors)
    payload={'schema_version':1,'updated_at':datetime.now(timezone.utc).isoformat(),'power':power,'gas':gas}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"power rows={len(power.get('curve',[]))}; gas rows={len(gas.get('curve',[]))}")
if __name__=='__main__': main()
