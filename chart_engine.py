import io
import base64
import pandas as pd
import matplotlib  # <--- 新增这行

# 【关键修复】设置后端为 Agg (非交互模式)
# 必须在 import matplotlib.pyplot 之前执行
matplotlib.use('Agg')

import mplfinance as mpf
import matplotlib.pyplot as plt


class ChartGenerator:
    def _calculate_indicators(self, df):
        # ... (代码保持不变) ...
        # 1. 基础均线
        df['MA30'] = df['close'].rolling(window=30).mean()
        df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()
        df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()

        # 2. 布林带 (BOLL)
        df['BOLL_MID'] = df['close'].rolling(window=20).mean()
        std = df['close'].rolling(window=20).std()
        df['BOLL_UP'] = df['BOLL_MID'] + 2 * std
        df['BOLL_LOW'] = df['BOLL_MID'] - 2 * std

        # 3. MACD
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp12 - exp26
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['MACD'] = (df['DIF'] - df['DEA']) * 2

        # 4. KDJ
        low_min = df['low'].rolling(window=9).min()
        high_max = df['high'].rolling(window=9).max()
        rsv = (df['close'] - low_min) / (high_max - low_min) * 100
        df['K'] = rsv.ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3 * df['K'] - 2 * df['D']

        # 5. RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        return df

    def _add_legend_text(self, ax, texts, y_pos=0.92):
        # ... (代码保持不变) ...
        x_cursor = 0.01
        for text, color in texts:
            t = ax.text(
                x_cursor, y_pos, text,
                transform=ax.transAxes,
                color=color,
                fontsize=8,
                fontweight='bold',
                verticalalignment='top'
            )
            renderer = ax.figure.canvas.get_renderer()
            bbox = t.get_window_extent(renderer)
            inv = ax.transAxes.inverted()
            bbox_axes = inv.transform(bbox)
            width = bbox_axes[1][0] - bbox_axes[0][0]
            x_cursor += width + 0.02

    def generate_chart_base64(self, df, title):
        if df.empty:
            return None

        df = self._calculate_indicators(df)
        last = df.iloc[-1]
        plot_data = df.tail(100)

        mc = mpf.make_marketcolors(
            up='#0ecb81', down='#f6465d',
            edge='inherit', wick='inherit',
            volume={'up': '#0ecb81', 'down': '#f6465d'}
        )
        s = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle='--',
            gridcolor='#d1d4dc',
            rc={
                'font.family': 'sans-serif',
                'font.size': 8,
                'axes.labelcolor': '#333',
                'axes.edgecolor': '#d1d4dc'
            }
        )

        add_plots = [
            mpf.make_addplot(plot_data['MA30'], color='#ffbc00', width=1.0),
            mpf.make_addplot(plot_data['EMA20'], color='#ab47bc', width=1.0),
            mpf.make_addplot(plot_data['EMA10'], color='#2962ff', width=1.0),
            mpf.make_addplot(plot_data['BOLL_UP'], color='#00897b', width=0.7),
            mpf.make_addplot(plot_data['BOLL_LOW'], color='#00897b', width=0.7),
            mpf.make_addplot(plot_data['DIF'], panel=2, color='#2962ff', width=1.0, ylabel='MACD'),
            mpf.make_addplot(plot_data['DEA'], panel=2, color='#ff6d00', width=1.0),
            mpf.make_addplot(plot_data['MACD'], panel=2, type='bar', color='gray', alpha=0.5),
            mpf.make_addplot(plot_data['K'], panel=3, color='#2962ff', width=1.0, ylabel='Oscillator'),
            mpf.make_addplot(plot_data['D'], panel=3, color='#ff6d00', width=1.0),
            mpf.make_addplot(plot_data['J'], panel=3, color='#e91e63', width=1.0),
            mpf.make_addplot(plot_data['RSI'], panel=3, color='#9c27b0', width=1.0, linestyle='--'),
        ]

        buf = io.BytesIO()

        # 为了线程安全，这里最好显式创建 figure (mplfinance 内部虽然处理了，但 Agg 模式下更稳)
        # 注意：returnfig=True 是必须的
        fig, axes = mpf.plot(
            plot_data,
            type='candle',
            style=s,
            addplot=add_plots,
            volume=True,
            datetime_format='%m-%d %H:%M',
            returnfig=True,
            panel_ratios=(5, 1, 1.5, 1.5),
            figsize=(10, 8),
            tight_layout=True,
            scale_padding={'top': 1}
        )

        # ... (Legend 绘制代码保持不变) ...
        price_color = '#0ecb81' if last['close'] >= last['open'] else '#f6465d'
        self._add_legend_text(axes[0], [
            (f"{title}", 'black'),
            (f"Price: {last['close']:.2f}", price_color)
        ], y_pos=0.96)

        self._add_legend_text(axes[0], [
            (f"MA30:{last['MA30']:.2f}", '#ffbc00'),
            (f"EMA20:{last['EMA20']:.2f}", '#ab47bc'),
            (f"EMA10:{last['EMA10']:.2f}", '#2962ff'),
            (f"BOLL:{last['BOLL_UP']:.2f}/{last['BOLL_MID']:.2f}/{last['BOLL_LOW']:.2f}", '#00897b')
        ], y_pos=0.90)

        vol_ax = axes[2]
        self._add_legend_text(vol_ax, [(f"VOL: {int(last['volume'])}", '#333333')])

        macd_ax = axes[4]
        self._add_legend_text(macd_ax, [
            ("MACD", 'black'),
            (f"DIF:{last['DIF']:.2f}", '#2962ff'),
            (f"DEA:{last['DEA']:.2f}", '#ff6d00'),
            (f"HIST:{last['MACD']:.2f}", '#666666')
        ])

        osc_ax = axes[6]
        self._add_legend_text(osc_ax, [
            (f"K:{last['K']:.2f}", '#2962ff'),
            (f"D:{last['D']:.2f}", '#ff6d00'),
            (f"J:{last['J']:.2f}", '#e91e63'),
            (f"RSI:{last['RSI']:.2f}", '#9c27b0')
        ])

        # 保存并清理
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.1)

        # 【重要】一定要清理内存，否则多线程长期运行会内存泄漏
        plt.close(fig)
        plt.close('all')  # 双重保险

        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        return img_base64