# Common Workflows

## Every morning (live trading)

```bash
cd ~/Documents/Dev/kite-algo
source .venv/bin/activate

# 1. Sync repo
git pull

# 2. Re-authenticate (Kite access tokens rotate at ~6am IST)
python -m kite_algo.kite_tool login

# 3. Sanity checks
python -m kite_algo.kite_tool profile
python -m kite_algo.kite_tool margins
python -m kite_algo.kite_tool positions

# 4. Optional: refresh instruments cache (Kite posts a new dump ~8:30am IST)
python -m kite_algo.kite_tool instruments --exchange NSE --refresh
python -m kite_algo.kite_tool instruments --exchange NFO --refresh
```

## Data exploration

```bash
# Top-of-book for a few symbols
python -m kite_algo.kite_tool ltp --symbols NSE:RELIANCE,NSE:INFY,NSE:TCS

# Full quote with depth
python -m kite_algo.kite_tool quote --symbols NSE:RELIANCE --flat

# Intraday 5-min bars for the last 3 days
python -m kite_algo.kite_tool history --symbol RELIANCE --exchange NSE --interval 5minute --days 3

# Search the instruments cache
python -m kite_algo.kite_tool search --query BANKNIFTY --exchange NFO --limit 20

# Full NIFTY option chain with live quotes
python -m kite_algo.kite_tool expiries --symbol NIFTY
python -m kite_algo.kite_tool chain --symbol NIFTY --expiry 2026-05-29 --quote --format csv > data/nifty_chain.csv
```

## Order management (when write path is live)

```bash
# Cancel one order
python -m kite_algo.kite_tool cancel --order-id 240101000123456 --yes

# Cancel everything open
python -m kite_algo.kite_tool cancel-all --yes

# Modify qty/price
python -m kite_algo.kite_tool modify --order-id 240101000123456 --quantity 100 --price 1250.50 --yes
```

## GTT management

```bash
# List active GTTs
python -m kite_algo.kite_tool gtt-list

# Get details for one
python -m kite_algo.kite_tool gtt-get --trigger-id 123456

# Delete
python -m kite_algo.kite_tool gtt-delete --trigger-id 123456 --yes
```

## Margin calculator

```bash
python -m kite_algo.kite_tool margin-calc --orders-json '[
  {
    "exchange": "NFO",
    "tradingsymbol": "NIFTY26MAY24000CE",
    "transaction_type": "SELL",
    "variety": "regular",
    "product": "NRML",
    "order_type": "LIMIT",
    "quantity": 50,
    "price": 125.50
  }
]'
```

## Mutual funds

```bash
python -m kite_algo.kite_tool mf-holdings
python -m kite_algo.kite_tool mf-sips
python -m kite_algo.kite_tool mf-orders
```

## End of day

```bash
# Archive today's orders + trades
python -m kite_algo.kite_tool orders --format csv > data/reports/orders_$(date +%Y%m%d).csv
python -m kite_algo.kite_tool trades --format csv > data/reports/trades_$(date +%Y%m%d).csv
python -m kite_algo.kite_tool positions --format json > data/reports/positions_$(date +%Y%m%d).json
```
