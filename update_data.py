import io, json, math, os, time
from datetime import date, timedelta
import requests
import pandas as pd

OUT='data/market.json'
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36'
S=requests.Session(); S.headers.update({'User-Agent':UA})

def get_eastmoney_index():
    url='https://push2his.eastmoney.com/api/qt/stock/kline/get'
    p={'secid':'1.000300','fields1':'f1,f2,f3','fields2':'f51,f52,f53,f54,f55,f56,f57','klt':'101','fqt':'0','beg':'20080101','end':'20991231','lmt':'6000','ut':'fa5fd1943c7b386f172d6893dbfba10b'}
    r=S.get(url,params=p,timeout=30); r.raise_for_status(); j=r.json(); ks=(j.get('data') or {}).get('klines') or []
    if not ks: raise RuntimeError('东方财富沪深300历史行情为空')
    rows=[]
    for k in ks:
        x=k.split(','); rows.append({'date':x[0],'index':float(x[2])})
    return pd.DataFrame(rows)

def get_csindex_dividend():
    # 中证指数公开的指数估值文件，包含日期、PE、股息率等字段。
    # 000300 为沪深300指数代码。
    url='https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator/000300indicator.xls'
    r=S.get(url,timeout=45); r.raise_for_status()
    df=pd.read_excel(io.BytesIO(r.content),engine='xlrd')
    if len(df.columns)<10: raise RuntimeError('中证指数估值文件字段异常')
    df=df.iloc[:,:10].copy(); df.columns=['date','code','name_cn','name_short','name_en','short_en','pe1','pe2','dividend1','dividend2']
    df['date']=pd.to_datetime(df['date'],errors='coerce').dt.strftime('%Y-%m-%d')
    df['dividend1']=pd.to_numeric(df['dividend1'],errors='coerce')
    df=df.dropna(subset=['date','dividend1'])[['date','dividend1']]
    # 中证指数文件的股息率字段单位为百分比，例如 2.80 表示 2.80%。
    if df.empty: raise RuntimeError('中证指数没有返回沪深300股息率历史数据')
    df['dividend_yield']=df['dividend1']/100.0
    return df[['date','dividend_yield']]

def get_bond():
    url='https://iftp.chinamoney.com.cn/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis'
    p={'lang':'CN','pageNum':1,'pageSize':5000}
    r=S.get(url,params=p,headers={'Referer':'https://iftp.chinamoney.com.cn/chinese/sddsintigy/'},timeout=45); r.raise_for_status(); j=r.json(); rec=j.get('records') or []
    if not rec: raise RuntimeError('中国货币网没有返回国债历史数据')
    rows=[]
    for x in rec:
        d=x.get('date') or x.get('tradeDate') or x.get('publishDate')
        y=x.get('tenRate')
        if d and y not in (None,''):
            try: rows.append({'date':str(d)[:10],'bond_yield':float(y)/100.0})
            except: pass
    df=pd.DataFrame(rows).drop_duplicates('date')
    if df.empty: raise RuntimeError('中国货币网10年期国债字段为空')
    return df

def main():
    print('1/3 获取东方财富沪深300...'); idx=get_eastmoney_index(); print(len(idx),idx.date.min(),idx.date.max())
    print('2/3 获取中证指数沪深300股息率...'); div=get_csindex_dividend(); print(len(div),div.date.min(),div.date.max())
    print('3/3 获取中国货币网10年期国债...'); bond=get_bond(); print(len(bond),bond.date.min(),bond.date.max())
    df=idx.merge(div,on='date',how='inner').merge(bond,on='date',how='inner').sort_values('date')
    if len(df)<100: raise RuntimeError(f'三类数据有效重合日期仅 {len(df)} 条，拒绝生成不完整数据文件')
    df['spread']=df['dividend_yield']-df['bond_yield']
    df=df.replace([math.inf,-math.inf],pd.NA).dropna()
    data=[]
    for _,r in df.iterrows(): data.append({'date':r['date'],'index':round(float(r['index']),4),'dividend_yield':round(float(r['dividend_yield']),8),'bond_yield':round(float(r['bond_yield']),8),'spread':round(float(r['spread']),8)})
    payload={'updated_at':pd.Timestamp.utcnow().isoformat(),'source':{'index':'Eastmoney','dividend':'CSI Index','bond':'ChinaMoney'},'data':data}
    tmp=OUT+'.tmp'; os.makedirs(os.path.dirname(OUT),exist_ok=True)
    with open(tmp,'w',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False,separators=(',',':'))
    os.replace(tmp,OUT)
    print('写入成功:',OUT,'records=',len(data))

if __name__=='__main__': main()
