"""Invented-but-realistic Telegram signal messages.

Real channels are chaotic, so the corpus mimics that: emojis, markdown, three
languages, "Target 1" instead of "TP", decimal commas, one-liners, adverts,
trade updates, and deliberately broken posts that MUST NOT be traded.

Each case is a dict:
  name, text, and either
    expect  -> parsed signal fields (symbol/side/entries/sl/tps/leverage/market)
    status  -> "ignored" | "rejected" (+ optional `reason` substring)
"""

VALID = [
    dict(
        name="clean canonical",
        text="BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP: 67000\nLeverage: 10x",
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["65000"], sl="64000",
                    tps=["67000"], leverage=10),
    ),
    dict(
        name="emoji chaos, entry zone, Target 1/2/3",
        text=(
            "🔥🔥 BTC/USDT 🟢 LONG 🔥🔥\n\n"
            "🎯 Entry zone: 64900 - 65100\n"
            "🎯 Target 1: 65800\n🎯 Target 2: 66500\n🎯 Target 3: 67500\n"
            "🛑 Stop: 64200\n⚡ Leverage: Cross 10x"
        ),
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["64900", "65100"], sl="64200",
                    tps=["65800", "66500", "67500"], leverage=10),
    ),
    dict(
        name="telegram markdown bold",
        text="**ETHUSDT** SHORT\n**Entry**: 3200\n**TP1**: 3100\n**TP2**: 3000\n**SL**: 3280\n**Leverage**: 5x",
        expect=dict(symbol="ETH-USDT", side="SHORT", entries=["3200"], sl="3280",
                    tps=["3100", "3000"], leverage=5),
    ),
    dict(
        name="ukrainian with Cyrillic 'х' in leverage",
        text="🟢 ЛОНГ SOLUSDT\nВхід: 150.5\nЦіль 1: 153\nЦіль 2: 156\nСтоп-лосс: 147\nПлече: 10х",
        expect=dict(symbol="SOL-USDT", side="LONG", entries=["150.5"], sl="147",
                    tps=["153", "156"], leverage=10),
    ),
    dict(
        name="russian with decimal commas",
        text="Шорт XRP/USDT\nВход по 0,52\nТейк-профит 1: 0,50\nТейк-профит 2: 0,48\nСтоп: 0,54\nПлечо 15x",
        expect=dict(symbol="XRP-USDT", side="SHORT", entries=["0.52"], sl="0.54",
                    tps=["0.50", "0.48"], leverage=15),
    ),
    dict(
        name="one-liner, no leverage",
        text="SOL long entry 150 sl 145 tp 160",
        expect=dict(symbol="SOL-USDT", side="LONG", entries=["150"], sl="145", tps=["160"],
                    leverage=None),
    ),
    dict(
        name="Buy zone, tgt1/tgt2/TP 3 - x, 'Stop loss -'",
        text=("#DOGEUSDT BUY\nBuy zone 0.1500-0.1520\ntgt1 0.1580\ntgt2 : 0.1650\n"
              "TP 3 - 0.1750\nStop loss - 0.1450"),
        expect=dict(symbol="DOGE-USDT", side="LONG", entries=["0.1500", "0.1520"], sl="0.1450",
                    tps=["0.1580", "0.1650", "0.1750"]),
    ),
    dict(
        name="thousands commas and slash-separated targets",
        text="BTCUSDT SHORT\nEntry 65,500\nTargets: 65,000 / 64,500 / 64,000\nSL 66,200",
        expect=dict(symbol="BTC-USDT", side="SHORT", entries=["65500"], sl="66200",
                    tps=["65000", "64500", "64000"]),
    ),
    dict(
        name="signal card with percentages, risk and time",
        text=(
            "📊 SIGNAL #1042\n🪙 Pair: ETH/USDT\n📉 Direction: SHORT\n💵 Entry: 3250 – 3270\n"
            "🎯 TP1: 3200 (+1.5%)\n🎯 TP2: 3150 (+3.1%)\n🛑 SL: 3320 (-1.8%)\n"
            "⚖️ Risk: 1-2%\n🕐 Time: 14:30"
        ),
        expect=dict(symbol="ETH-USDT", side="SHORT", entries=["3250", "3270"], sl="3320",
                    tps=["3200", "3150"]),
    ),
    dict(
        name="market entry",
        text="🔥 AVAX LONG NOW 🔥\nEntry: market\nSL: 32.5\nTP1: 35\nTP2: 37\nLev 10x",
        expect=dict(symbol="AVAX-USDT", side="LONG", entries=[], sl="32.5", tps=["35", "37"],
                    leverage=10, market=True),
    ),
    dict(
        name="inline with x20 and slash targets",
        text="#ADA/USDT SHORT 🔻 Entry 0.45 TP 0.43 / 0.41 SL 0.47 x20",
        expect=dict(symbol="ADA-USDT", side="SHORT", entries=["0.45"], sl="0.47",
                    tps=["0.43", "0.41"], leverage=20),
    ),
    dict(
        name="'Take Profit' / 'Stop Loss' words",
        text="LTCUSDT LONG\nEntry: 85.5\nTake Profit: 88\nStop Loss: 83",
        expect=dict(symbol="LTC-USDT", side="LONG", entries=["85.5"], sl="83", tps=["88"]),
    ),
    dict(
        name="numbered target list",
        text="Long ARB/USDT\nEntry 0.80\nTargets:\n1) 0.82\n2) 0.84\n3) 0.86\nStop 0.77",
        expect=dict(symbol="ARB-USDT", side="LONG", entries=["0.80"], sl="0.77",
                    tps=["0.82", "0.84", "0.86"]),
    ),
    dict(
        name="all lowercase, space-separated targets",
        text="btcusdt long entry 65000 stop 64000 target 66000 67000 68000",
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["65000"], sl="64000",
                    tps=["66000", "67000", "68000"]),
    ),
    dict(
        name="Enter at / Take-profit 1 / Stoploss / leverage range",
        text=("SUI/USDT SHORT\nEnter at 3.80\nTake-profit 1: 3.70\nTake-profit 2: 3.60\n"
              "Stoploss: 3.90\nLeverage 10-20x"),
        expect=dict(symbol="SUI-USDT", side="SHORT", entries=["3.80"], sl="3.90",
                    tps=["3.70", "3.60"], leverage=10),
    ),
    dict(
        name="isolated margin line and position-size noise",
        text="BNBUSDT LONG\nEntry 600\nTP 620\nSL 590\nMargin: Isolated 25x\nSize: 5% of deposit",
        expect=dict(symbol="BNB-USDT", side="LONG", entries=["600"], sl="590", tps=["620"],
                    leverage=25),
    ),
    dict(
        name="advert footer after the signal",
        text=("BTCUSDT LONG\nEntry 65000\nTP 66000\nSL 64500\n\n"
              "⚠️ Not financial advice. Join VIP: t.me/vipchannel 💎 24/7 support"),
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["65000"], sl="64500", tps=["66000"]),
    ),
    dict(
        name="full-width unicode characters",
        text="ＢＴＣＵＳＤＴ ＬＯＮＧ Ｅｎｔｒｙ： ６５０００ ＳＬ： ６４０００ ＴＰ： ６６０００",
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["65000"], sl="64000", tps=["66000"]),
    ),
    dict(
        name="dollar signs",
        text="BTC LONG\nEntry: $65,000\nSL: $64,000\nTP: $67,000",
        expect=dict(symbol="BTC-USDT", side="LONG", entries=["65000"], sl="64000", tps=["67000"]),
    ),
    dict(
        name="DCA entries (Entry 1 / Entry 2)",
        text="ETH LONG\nEntry 1: 3000\nEntry 2: 2950\nSL: 2850\nTP1: 3100\nTP2: 3200",
        expect=dict(symbol="ETH-USDT", side="LONG", entries=["2950", "3000"], sl="2850",
                    tps=["3100", "3200"]),
    ),
    dict(
        name="stop first, symbol as hashtag",
        text="🛑 SL 1.95\n🎯 TP 2.10\n🟢 LONG #LINK\n📥 Entry 2.00",
        expect=dict(symbol="LINK-USDT", side="LONG", entries=["2.00"], sl="1.95", tps=["2.10"]),
    ),
    dict(
        name="perpetual suffix .P",
        text="BTCUSDT.P SHORT\nEntry 66000\nTP 65000\nSL 66500",
        expect=dict(symbol="BTC-USDT", side="SHORT", entries=["66000"], sl="66500", tps=["65000"]),
    ),
    dict(
        name="1000-prefixed meme coin",
        text="1000PEPEUSDT LONG\nEntry 0.009500\nTP 0.010000\nSL 0.009200",
        expect=dict(symbol="1000PEPE-USDT", side="LONG", entries=["0.009500"], sl="0.009200",
                    tps=["0.010000"]),
    ),
    dict(
        name="'Target 1 <price>' without colon (price >= 100)",
        text="ETH SHORT\nEntry 3200\nTarget 1 3100\nTarget 2 3000\nSL 3300",
        expect=dict(symbol="ETH-USDT", side="SHORT", entries=["3200"], sl="3300",
                    tps=["3100", "3000"]),
    ),
    dict(
        name="'Target 1 <price>' without colon on a cheap coin (sequential indexes)",
        text="ADA LONG\nEntry 0.45\nTarget 1 0.47\nTarget 2 0.49\nSL 0.43",
        expect=dict(symbol="ADA-USDT", side="LONG", entries=["0.45"], sl="0.43",
                    tps=["0.47", "0.49"]),
    ),
]

IGNORED = [
    dict(name="TP hit announcement", text="✅ BTCUSDT TP1 reached! +3.2% profit 🔥", reason=""),
    dict(name="good morning chatter", text="Good morning traders! Market looks bullish today 🌞"),
    dict(name="update: move SL to breakeven",
         text="BTC update: moving SL to breakeven, TP1 hit. Keep holding"),
    dict(name="promo", text="🚨 VIP SALE -50% today only! Join now"),
    dict(name="weekly recap mentions both sides",
         text="Results for the week: ETHUSDT LONG +12%, SOLUSDT SHORT +8%, BTCUSDT LONG +5%"),
    dict(name="empty", text="   "),
    dict(name="emoji only", text="🔥🔥🔥"),
    dict(name="long-term outlook", text="Long-term outlook for BTC: I'm bullish. Entry points soon."),
    dict(name="calculator promo", text="Try our SL/TP calculator bot!"),
]

REJECTED = [
    dict(name="missing stop-loss", text="BTCUSDT LONG Entry 65000 TP 67000", reason="stop-loss"),
    dict(name="missing take-profit", text="BTCUSDT LONG Entry 65000 SL 64000", reason="take-profit"),
    dict(name="missing entry (not market)", text="BTCUSDT LONG SL 64000 TP 67000", reason="entry"),
    dict(name="conflicting direction",
         text="BTCUSDT LONG or SHORT? Entry 65000 SL 64000 TP 67000", reason="direction"),
    dict(name="two symbols",
         text="BTC/USDT and ETH/USDT LONG Entry 65000 SL 64000 TP 67000", reason="symbol"),
    dict(name="conflicting stop-loss values",
         text="BTCUSDT LONG Entry 65000 SL 64000 SL 63000 TP 67000", reason="stop-loss"),
    dict(name="two values after one SL", text="BTCUSDT LONG Entry 65000 SL 64000 63000 TP 67000",
         reason="stop-loss"),
    dict(name="symbol missing", text="LONG Entry 65000 SL 64000 TP 67000", reason="symbol"),
    dict(name="stop-loss only in percent", text="BTCUSDT LONG Entry 65000 SL 2% TP 67000",
         reason="stop-loss"),
    dict(name="unsupported quote currency (USDC)",
         text="BTC/USDC LONG Entry 65000 SL 64000 TP 67000", reason="symbol"),
    dict(name="direction word missing", text="BTCUSDT Entry 65000 SL 64000 TP 67000",
         reason="direction"),
]
