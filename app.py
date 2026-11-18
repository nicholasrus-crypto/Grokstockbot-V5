import os, json, sqlite3, time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

APP_TITLE = "GrokStockBot NIGHT RUN"
DB = os.getenv("DB_PATH", "grokstockbot.db")
DEX = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
EUR_USD_FALLBACK = 1.08

DEFAULTS = {
    "capital_eur": 1000.0,
    "risk_per_trade_pct": 0.5,
    "daily_loss_limit_pct": 1.0,
    "max_open_positions": 2,
    "max_trade_pct": 5.0,
    "min_liquidity_usd": 50000,
    "min_volume_24h_usd": 100000,
    "min_pair_age_hours": 24,
    "max_pair_age_hours": 720,
    "max_price_change_24h_pct": 250,
    "min_txns_24h": 150,
    "min_score": 75,
    "min_reward_risk": 2.0,
    "stop_loss_pct": 8.0,
    "take_profit_pct": 16.0,
    "max_slippage_bps": 100,
    "trade_window_start": 0,
    "trade_window_end": 8,
}
def conn():
    c=sqlite3.connect(DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS proposals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, mint TEXT, symbol TEXT,
      entry_usd REAL, amount_eur REAL, stop_usd REAL, target_usd REAL,
      score INTEGER, status TEXT, notes TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS positions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, entry_usd REAL,
      amount_eur REAL, stop_usd REAL, target_usd REAL, opened TEXT,
      status TEXT, exit_usd REAL, pnl_eur REAL, mode TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, event TEXT, details TEXT)""")
    c.commit()
    return c
def log(c,event,details=""):
    c.execute("INSERT INTO events(created,event,details) VALUES(?,?,?)",
              (datetime.now(timezone.utc).isoformat(),event,str(details)))
    c.commit()
def get_pairs():
    r=requests.get(DEX+"/token-profiles/latest/v1",timeout=15)
    r.raise_for_status()
    items=[x for x in r.json() if x.get("chainId")=="solana"]
    pairs=[]
    seen=set()
    for x in items[:120]:
        addr=x.get("tokenAddress")
        if not addr or addr in seen: continue
        seen.add(addr)
        try:
            q=requests.get(f"{DEX}/token-pairs/v1/solana/{addr}",timeout=12)
            if q.ok:
                pairs.extend(q.json() or [])
        except requests.RequestException:
            pass
        time.sleep(.12)
    return pairs
def score_pair(p):
    liq=float((p.get("liquidity") or {}).get("usd") or 0)
    vol=float((p.get("volume") or {}).get("h24") or 0)
    tx=p.get("txns") or {}; h24=tx.get("h24") or {}
    txns=int(h24.get("buys") or 0)+int(h24.get("sells") or 0)
    ch=float((p.get("priceChange") or {}).get("h24") or 0)
    created=p.get("pairCreatedAt")
    age=(time.time()*1000-float(created))/3600000 if created else 0
    score=0; reasons=[]
    if liq>=100000: score+=25; reasons.append("strong liquidity")
    elif liq>=50000: score+=18; reasons.append("acceptable liquidity")
    if vol>=250000: score+=20; reasons.append("strong 24h volume")
    elif vol>=100000: score+=14; reasons.append("acceptable 24h volume")
    if txns>=500: score+=20; reasons.append("active trading")
    elif txns>=150: score+=12; reasons.append("adequate activity")
    if 24<=age<=720: score+=15; reasons.append("age within filter")
    if -15<=ch<=120: score+=10; reasons.append("price movement not extreme")
    if float((p.get("liquidity") or {}).get("usd") or 0)>0 and vol/max(liq,1)>0.5: score+=10; reasons.append("volume/liquidity ratio")
    return score, liq, vol, txns, age, ch, "; ".join(reasons)
def scan():
    out=[]
    for p in get_pairs():
        score,liq,vol,txns,age,ch,why=score_pair(p)
        base=p.get("baseToken") or {}
        price=float(p.get("priceUsd") or 0)
        if not price: continue
        if liq<DEFAULTS["min_liquidity_usd"] or vol<DEFAULTS["min_volume_24h_usd"]: continue
        if age<DEFAULTS["min_pair_age_hours"] or age>DEFAULTS["max_pair_age_hours"]: continue
        if txns<DEFAULTS["min_txns_24h"] or ch>DEFAULTS["max_price_change_24h_pct"]: continue
        if score<DEFAULTS["min_score"]: continue
        out.append({"symbol":base.get("symbol","?"),"name":base.get("name",""),"mint":base.get("address",""),
                    "price_usd":price,"liquidity_usd":liq,"volume_24h_usd":vol,"txns_24h":txns,
                    "age_hours":round(age,1),"change_24h_pct":ch,"score":score,"reasons":why,
                    "url":p.get("url","")})
    return sorted(out,key=lambda x:x["score"],reverse=True)[:30]
def eur_usd():
    try:
        j=requests.get("https://api.frankfurter.app/latest?from=EUR&to=USD",timeout=6).json()
        return float(j["rates"]["USD"])
    except Exception: return EUR_USD_FALLBACK
def risk_state(c, capital):
    today=datetime.now(timezone.utc).date().isoformat()
    rows=c.execute("SELECT pnl_eur FROM positions WHERE status='CLOSED' AND substr(opened,1,10)=?",(today,)).fetchall()
    loss=sum(min(0,float(x[0] or 0)) for x in rows)
    return loss, max(0,capital*DEFAULTS["daily_loss_limit_pct"]/100+loss)
def make_proposal(c, token, capital, mode):
    open_n=c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    if open_n>=DEFAULTS["max_open_positions"]: raise ValueError("Maximum open positions reached.")
    loss,remaining=risk_state(c,capital)
    if remaining<=0: raise ValueError("Daily loss limit reached. Trading locked until next UTC day.")
    eurusd=eur_usd()
    risk_eur=capital*DEFAULTS["risk_per_trade_pct"]/100
    stop_pct=DEFAULTS["stop_loss_pct"]/100
    size_eur=min(risk_eur/stop_pct,capital*DEFAULTS["max_trade_pct"]/100,remaining/stop_pct)
    size_eur=round(size_eur,2)
    if size_eur<=0: raise ValueError("Risk budget is exhausted.")
    entry=token["price_usd"]
    stop=entry*(1-stop_pct)
    target=entry*(1+DEFAULTS["take_profit_pct"]/100)
    if (target-entry)/(entry-stop)<DEFAULTS["min_reward_risk"]: raise ValueError("Reward/risk below threshold.")
    cur=c.execute("""INSERT INTO proposals(created,mint,symbol,entry_usd,amount_eur,stop_usd,target_usd,score,status,notes)
       VALUES(?,?,?,?,?,?,?,?,?,?)""",(datetime.now(timezone.utc).isoformat(),token["mint"],token["symbol"],entry,size_eur,stop,target,token["score"],"PENDING",token["reasons"]))
    c.commit()
    return cur.lastrowid,size_eur,eurusd,stop,target
def jupiter_url(mint,eur_amount,eurusd):
    # URL opens Jupiter for a human-reviewed swap; EUR is converted to approximate USD then SOL.
    # Final amount/route/fees must be checked in wallet before signing.
    usd=eur_amount*eurusd
    return f"https://jup.ag/swap/SOL-{mint}?inAmount={max(0,int(usd*1e9/150))}"
st.set_page_config(page_title=APP_TITLE,layout="wide")
st.title("ð GrokStockBot NIGHT RUN")
st.caption("Solana scanner â¢ strict risk limits â¢ manual approval only â¢ 00:00â08:00 UTC")
c=conn()
capital=st.sidebar.number_input("Starting capital (â¬)",min_value=50.0,max_value=100000.0,value=1000.0,step=50.0)
st.sidebar.info("Default mode: PAPER. Live trades are not signed or sent by this app.")
mode=st.sidebar.radio("Trading mode",["PAPER","LIVE MANUAL"],index=0)
st.sidebar.markdown("**Risk rules**")
st.sidebar.write("Risk/trade: 0.5% (â¬5 at â¬1,000)")
st.sidebar.write("Daily loss cap: 1% (â¬10)")
st.sidebar.write("Max positions: 2")
st.sidebar.write("Max position size: 5% (â¬50)")
st.sidebar.write("Stop: 8% â¢ Target: 16%")
st.sidebar.write("Min score: 75/100")
loss,remaining=risk_state(c,capital)
a,b,d=st.columns(3)
a.metric("Starting capital",f"â¬{capital:,.2f}")
b.metric("Daily loss remaining",f"â¬{remaining:,.2f}")
d.metric("Open positions",c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0])
if mode=="LIVE MANUAL":
    st.warning("LIVE MANUAL is a proposal workflow only. This app does not sign, submit, or custody transactions. Review and sign each swap yourself in your wallet. After execution, record the actual fill below.")
if st.button("ð Scan Solana tokens now",type="primary"):
    with st.spinner("Fetching and scoring token pairs..."):
        try:
            st.session_state["scan_results"]=scan()
            log(c,"SCAN",f"{len(st.session_state['scan_results'])} candidates")
        except Exception as e: st.error(f"Scan failed: {e}")
results=st.session_state.get("scan_results",[])
if results:
    df=pd.DataFrame(results)
    st.subheader("Qualified candidates")
    st.dataframe(df[["symbol","price_usd","liquidity_usd","volume_24h_usd","txns_24h","age_hours","change_24h_pct","score"]],use_container_width=True)
    choices={f"{x['symbol']} | score {x['score']} | {x['mint'][:7]}â¦":x for x in results}
    selected=st.selectbox("Select a candidate to prepare a trade proposal",list(choices))
    token=choices[selected]
    st.write("Filter evidence:",token["reasons"])
    if st.button("Prepare trade proposal"):
        try:
            pid,size,fx,stop,target=make_proposal(c,token,capital,mode)
            st.session_state["last_proposal"]={"id":pid,"token":token,"size":size,"fx":fx,"stop":stop,"target":target,"mode":mode}
        except Exception as e: st.error(str(e))
proposal=st.session_state.get("last_proposal")
if proposal:
    t=proposal["token"]
    st.subheader(f"Trade proposal #{proposal['id']} â¢ {t['symbol']}")
    st.write(f"Indicative size: â¬{proposal['size']:.2f} | Entry: ${t['price_usd']:.8g} | Stop: ${proposal['stop']:.8g} | Target: ${proposal['target']:.8g}")
    st.write("This is a signal, not a guarantee. Token contract, liquidity, authorities, holders, slippage and route must be independently checked.")
    if proposal["mode"]=="PAPER":
        if st.button("Approve PAPER trade"):
            c.execute("UPDATE proposals SET status='APPROVED_PAPER' WHERE id=?",(proposal["id"],))
            c.execute("""INSERT INTO positions(mint,symbol,entry_usd,amount_eur,stop_usd,target_usd,opened,status,mode)
                         VALUES(?,?,?,?,?,?,?,?,?)""",(t["mint"],t["symbol"],t["price_usd"],proposal["size"],proposal["stop"],proposal["target"],datetime.now(timezone.utc).isoformat(),"OPEN","PAPER"))
            c.commit(); log(c,"PAPER_OPEN",t["mint"]); st.success("Paper position opened.")
    else:
        st.link_button("Open token swap in Jupiter (review and sign yourself)",f"https://jup.ag/swap/SOL-{t['mint']}")
        st.caption("The link is only a convenience. Verify token mint, exact SOL amount, output, price impact, fees and slippage in your wallet. Never approve a transaction you do not understand.")
        if st.checkbox("I independently checked the mint, route, amount and risks"):
            if st.button("Record that I approved this trade externally"):
                c.execute("UPDATE proposals SET status='USER_APPROVED_EXTERNALLY' WHERE id=?",(proposal["id"],)); c.commit()
                st.info("Approval recorded only. No transaction was sent by GrokStockBot. Record the actual fill below after signing in your wallet.")
st.subheader("Record a manually executed live trade")
with st.form("live_fill"):
    pid=st.number_input("Proposal ID",min_value=1,step=1)
    actual_entry=st.number_input("Actual entry price (USD)",min_value=0.0,format="%.10f")
    actual_size=st.number_input("Actual amount invested (EUR)",min_value=0.0,step=1.0)
    submitted=st.form_submit_button("Record live fill")
    if submitted:
        p=c.execute("SELECT mint,symbol,stop_usd,target_usd,status FROM proposals WHERE id=?",(pid,)).fetchone()
        if not p or p[4] not in ("USER_APPROVED_EXTERNALLY","PENDING"): st.error("Proposal not found or not in a recordable state.")
        elif actual_entry<=0 or actual_size<=0: st.error("Enter valid fill values.")
        else:
            c.execute("""INSERT INTO positions(mint,symbol,entry_usd,amount_eur,stop_usd,target_usd,opened,status,mode)
                         VALUES(?,?,?,?,?,?,?,?,?)""",(p[0],p[1],actual_entry,actual_size,actual_entry*(1-DEFAULTS["stop_loss_pct"]/100),actual_entry*(1+DEFAULTS["take_profit_pct"]/100),datetime.now(timezone.utc).isoformat(),"OPEN","LIVE_MANUAL"))
            c.execute("UPDATE proposals SET status='FILLED_MANUALLY' WHERE id=?",(pid,)); c.commit(); log(c,"LIVE_FILL_RECORDED",pid); st.success("Fill recorded. Monitor and close the position manually in your wallet.")
st.subheader("Open positions")
pos=c.execute("SELECT id,symbol,mint,entry_usd,amount_eur,stop_usd,target_usd,mode,opened FROM positions WHERE status='OPEN'").fetchall()
if pos:
    st.dataframe(pd.DataFrame(pos,columns=["ID","Symbol","Mint","Entry USD","Invested EUR","Stop USD","Target USD","Mode","Opened"]),use_container_width=True)
    with st.form("close_position"):
        close_id=st.number_input("Position ID to close",min_value=1,step=1)
        exit_price=st.number_input("Actual exit price USD",min_value=0.0,format="%.10f")
        close=st.form_submit_button("Record position close")
        if close:
            row=c.execute("SELECT entry_usd,amount_eur,opened FROM positions WHERE id=? AND status='OPEN'",(close_id,)).fetchone()
            if not row or exit_price<=0: st.error("Invalid open position or exit price.")
            else:
                pnl=row[1]*(exit_price/row[0]-1)
                c.execute("UPDATE positions SET status='CLOSED',exit_usd=?,pnl_eur=? WHERE id=?",(exit_price,pnl,close_id)); c.commit()
                log(c,"POSITION_CLOSED",f"id={close_id}, pnl={pnl:.2f}"); st.success(f"Recorded P/L: â¬{pnl:.2f} (estimate, excludes fees/tax).")
else: st.info("No open positions.")
st.subheader("Recent proposals")
rows=c.execute("SELECT id,created,symbol,amount_eur,score,status FROM proposals ORDER BY id DESC LIMIT 20").fetchall()
if rows: st.dataframe(pd.DataFrame(rows,columns=["ID","Created UTC","Symbol","Size EUR","Score","Status"]),use_container_width=True)
st.divider()
st.caption("Scanner limitations: DexScreener profile/pair data is not a contract audit. This version does not verify mint/freeze authorities, holder concentration, liquidity locks, honeypots or sellability. Do not use it as a live safety certification. No automated overnight service is included; this dashboard scans only when you press the scan button.")
