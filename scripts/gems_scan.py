import json
import pandas as pd
from src import portfolio
CAPS=[0.5,0.67,0.8,0.9,1.0]; LAMBDAS=[0.5,0.75,1.0,1.5,2.0]; LOOKBACKS=[14,30,60,90,120,150]
_FM=portfolio.load_financial_model()                       # anchors + rf/lag from the profile (cap/λ/lb are swept)
ANCHORS=_FM.get('always_include') or ['SPY','AGG','IAU']; ANC=set(ANCHORS)
_RF=float(_FM['risk_free_rate']); _TU=int(portfolio.load_backtest_config()['t_update_days'])
MWS=[(2,'gkg-3yr-mws2'),(3,'gkg-3yr-mws3'),(4,'gkg-3yr-mws4'),(5,'gkg-3yr-final'),(6,'gkg-3yr-mws6'),
     (7,'gkg-3yr-mws7'),(8,'gkg-3yr-mws8'),(10,'gkg-3yr-mws10'),(12,'gkg-3yr-mws12')]
best={}; n_ok=0; n_fail=0
for mws,d in MWS:
    rd=f'data/curator_runs/{d}'
    for cap in CAPS:
        for lam in LAMBDAS:
            for lb in LOOKBACKS:
                out=f'/tmp/_gems/{mws}_{cap}_{lam}_{lb}'
                try:
                    portfolio.curator_backtest(runs_dir=rd,out_dir=out,max_weight=cap,risk_aversion=lam,
                        risk_free_rate=_RF,t_update_days=_TU,
                        benchmarks=[],lookback_years_override=lb/365.0,always_include=ANCHORS)
                    sn=pd.read_csv(f'{out}/snapshots.csv',parse_dates=['date'])
                    n_ok+=1
                except Exception:
                    n_fail+=1; continue
                for t,g in sn.groupby('ticker'):
                    if t in ANC: continue
                    g=g.sort_values('date'); sh=g['shares'].values; pr=g['price'].values
                    if len(sh)<2: continue
                    pnl=float((sh[:-1]*(pr[1:]-pr[:-1])).sum())
                    if t not in best or pnl>best[t]['gain']:
                        held=g[g['shares']>0]
                        pr0=held['price'].iloc[0] if len(held) else 0
                        pr1=held['price'].iloc[-1] if len(held) else 0
                        best[t]={'ticker':t,'gain':pnl,'mws':mws,'cap':cap,'lam':lam,'lb':lb,
                                 'days_held':int((g['shares']>0).sum()),
                                 'span':[str(g['date'].min().date()),str(g['date'].max().date())],
                                 'price_ret':float(pr1/pr0-1) if pr0 else 0.0}
    print(f'  done mws{mws} | ok={n_ok} fail={n_fail} | tickers so far={len(best)}',flush=True)
gems=sorted(best.values(),key=lambda r:-r['gain'])
json.dump(gems,open('data/curator_runs/_gems.json','w'),indent=1)
print(f'GEMS_DONE {len(gems)} tickers | ok={n_ok} fail={n_fail}',flush=True)
