from __future__ import annotations
import json, os, re, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import xml.etree.ElementTree as ET
import requests

OUT=Path('data/eu_day_ahead_prices.json')
API='https://web-api.tp.entsoe.eu/api'

# Verified/common ENTSO-E bidding-zone EICs. Countries not listed remain grey on the map.
COUNTRIES={
 'AT': {'country':'Austria','iso3':'AUT','zones':{'AT':'10YAT-APG------L'}},
 'BE': {'country':'Belgium','iso3':'BEL','zones':{'BE':'10YBE----------2'}},
 'BG': {'country':'Bulgaria','iso3':'BGR','zones':{'BG':'10YCA-BULGARIA-R'}},
 'HR': {'country':'Croatia','iso3':'HRV','zones':{'HR':'10YHR-HEP------M'}},
 'CZ': {'country':'Czechia','iso3':'CZE','zones':{'CZ':'10YCZ-CEPS-----N'}},
 'DE': {'country':'Germany/Luxembourg','iso3':'DEU','zones':{'DE-LU':'10Y1001A1001A82H'}},
 'LU': {'country':'Luxembourg','iso3':'LUX','zones':{'DE-LU':'10Y1001A1001A82H'}},
 'DK': {'country':'Denmark','iso3':'DNK','zones':{'DK1':'10YDK-1--------W','DK2':'10YDK-2--------M'}},
 'EE': {'country':'Estonia','iso3':'EST','zones':{'EE':'10Y1001A1001A39I'}},
 'ES': {'country':'Spain','iso3':'ESP','zones':{'ES':'10YES-REE------0'}},
 'FI': {'country':'Finland','iso3':'FIN','zones':{'FI':'10YFI-1--------U'}},
 'FR': {'country':'France','iso3':'FRA','zones':{'FR':'10YFR-RTE------C'}},
 'GR': {'country':'Greece','iso3':'GRC','zones':{'GR':'10YGR-HTSO-----Y'}},
 'HU': {'country':'Hungary','iso3':'HUN','zones':{'HU':'10YHU-MAVIR----U'}},
 'LV': {'country':'Latvia','iso3':'LVA','zones':{'LV':'10YLV-1001A00074'}},
 'LT': {'country':'Lithuania','iso3':'LTU','zones':{'LT':'10YLT-1001A0008Q'}},
 'NL': {'country':'Netherlands','iso3':'NLD','zones':{'NL':'10YNL----------L'}},
 'PL': {'country':'Poland','iso3':'POL','zones':{'PL':'10YPL-AREA-----S'}},
 'PT': {'country':'Portugal','iso3':'PRT','zones':{'PT':'10YPT-REN------W'}},
 'RO': {'country':'Romania','iso3':'ROU','zones':{'RO':'10YRO-TEL------P'}},
 'SK': {'country':'Slovakia','iso3':'SVK','zones':{'SK':'10YSK-SEPS-----K'}},
 'SI': {'country':'Slovenia','iso3':'SVN','zones':{'SI':'10YSI-ELES-----O'}},
 'SE': {'country':'Sweden','iso3':'SWE','zones':{
     'SE1':'10Y1001A1001A44P','SE2':'10Y1001A1001A45N','SE3':'10Y1001A1001A46L','SE4':'10Y1001A1001A47J'}},
}

def lname(tag): return tag.rsplit('}',1)[-1]
def dur_hours(s):
    m=re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?',s or '')
    if not m: return 1.0
    return (int(m.group(1) or 0)*60+int(m.group(2) or 0))/60

def parse_prices(xml_text):
    root=ET.fromstring(xml_text)
    if 'Acknowledgement' in lname(root.tag): return []
    pts=[]
    for ts in [x for x in root.iter() if lname(x.tag)=='TimeSeries']:
        for period in [x for x in ts if lname(x.tag)=='Period']:
            start=None; resolution='PT1H'
            for x in period.iter():
                n=lname(x.tag)
                if n=='start' and start is None:
                    try: start=datetime.fromisoformat((x.text or '').replace('Z','+00:00'))
                    except: pass
                elif n=='resolution' and x.text: resolution=x.text
            if start is None: continue
            step=dur_hours(resolution)
            for point in [x for x in period if lname(x.tag)=='Point']:
                pos=price=None
                for x in point:
                    n=lname(x.tag)
                    if n=='position':
                        try: pos=int(x.text)
                        except: pass
                    elif n=='price.amount':
                        try: price=float(x.text)
                        except: pass
                if pos and price is not None:
                    pts.append((start+timedelta(hours=(pos-1)*step), price, step))
    return pts

def fetch_zone(token,eic,start,end):
    p={'securityToken':token,'documentType':'A44','in_Domain':eic,'out_Domain':eic,
       'periodStart':start.strftime('%Y%m%d%H%M'),'periodEnd':end.strftime('%Y%m%d%H%M')}
    r=requests.get(API,params=p,timeout=(5,25),headers={'User-Agent':'BalticPulse/16 day-ahead map'})
    if r.status_code!=200: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:180]}')
    return parse_prices(r.text)

MARKET_TZ=ZoneInfo('Europe/Brussels')
TALLINN_TZ=ZoneInfo('Europe/Tallinn')

def daily_avg(points, day):
    vals=[(p,h) for t,p,h in points if t.astimezone(MARKET_TZ).date()==day]
    if not vals: return None
    den=sum(h for _,h in vals)
    return sum(p*h for p,h in vals)/den if den else None

def main():
    token=os.environ.get('ENTSOE_API_KEY','').strip()
    if not token: raise SystemExit('ENTSOE_API_KEY missing')
    now_local=datetime.now(TALLINN_TZ); days=[now_local.date(), now_local.date()+timedelta(days=1)]
    start=datetime.combine(days[0],datetime.min.time(),tzinfo=MARKET_TZ).astimezone(timezone.utc)-timedelta(hours=1)
    end=datetime.combine(days[-1]+timedelta(days=1),datetime.min.time(),tzinfo=MARKET_TZ).astimezone(timezone.utc)+timedelta(hours=1)
    cache={}; errors=[]; result={d.isoformat():{'countries':[]} for d in days}
    for cc,meta in COUNTRIES.items():
        zone_avgs={d.isoformat():[] for d in days}; zone_labels=[]
        for z,eic in meta['zones'].items():
            zone_labels.append(z)
            try:
                if eic not in cache: cache[eic]=fetch_zone(token,eic,start,end); time.sleep(0.05)
                for d in days:
                    av=daily_avg(cache[eic],d)
                    if av is not None: zone_avgs[d.isoformat()].append(av)
            except Exception as exc:
                errors.append(f'{z}: {type(exc).__name__}: {exc}')
        for d in days:
            vals=zone_avgs[d.isoformat()]
            if vals:
                result[d.isoformat()]['countries'].append({
                    'country':meta['country'],'iso3':meta['iso3'],'price_eur_mwh':round(sum(vals)/len(vals),2),
                    'zones':', '.join(zone_labels),'zone_count':len(vals)})
    previous={}
    if OUT.exists():
        try: previous=json.loads(OUT.read_text(encoding='utf-8'))
        except: pass
    # Preserve previous populated day if a total API failure occurs.
    for d in result:
        if not result[d]['countries'] and previous.get('days',{}).get(d,{}).get('countries'):
            result[d]=previous['days'][d]
    payload={'schema_version':1,'updated_at':datetime.now(timezone.utc).isoformat(),
             'method':'A44 day-ahead; country arithmetic mean across available bidding zones',
             'days':result,'errors':errors}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print({d:len(v['countries']) for d,v in result.items()})
if __name__=='__main__': main()
