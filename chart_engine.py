import io
import base64
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt


class ChartGenerator:
    def _calculate_indicators(self, df):
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

        # 4. KDJ (引入 RSV 计算)
        low_min = df['low'].rolling(window=9).min()
        high_max = df['high'].rolling(window=9).max()
        rsv = (df['close'] - low_min) / (high_max - low_min) * 100
        # fillna 0 防止开头数据为空报错
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
        """
        在子图上方绘制彩色图例
        texts: list of tuples (text_content, color_hex)
        """
        x_cursor = 0.01  # 初始 x 坐标 (相对坐标 0-1)
        for text, color in texts:
            t = ax.text(
                x_cursor, y_pos, text,
                transform=ax.transAxes,  # 使用相对坐标系
                color=color,
                fontsize=8,
                fontweight='bold',
                verticalalignment='top'
            )
            # 简单估算下一个文字的偏移量，Matplotlib静态渲染很难精确获取渲染后宽度，
            # 这里根据字符长度做一个经验值的偏移
            renderer = ax.figure.canvas.get_renderer()
            bbox = t.get_window_extent(renderer)
            # 将像素宽度转换为轴坐标宽度
            inv = ax.transAxes.inverted()
            bbox_axes = inv.transform(bbox)
            width = bbox_axes[1][0] - bbox_axes[0][0]

            x_cursor += width + 0.02  # 加上一点间距

    def generate_chart_base64(self, df, title):
        if df.empty:
            return None

        # 计算指标
        df = self._calculate_indicators(df)

        # 获取最后一行数据用于显示 Legend
        last = df.iloc[-1]

        # 截取最近数据绘图 (例如最近 80 根，太长会导致蜡烛太细看不清)
        plot_data = df.tail(100)

        # --- 样式设置 ---
        # 自定义颜色风格
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

        # --- 添加图表 ---
        add_plots = [
            # 主图指标
            mpf.make_addplot(plot_data['MA30'], color='#ffbc00', width=1.0),
            mpf.make_addplot(plot_data['EMA20'], color='#ab47bc', width=1.0),
            mpf.make_addplot(plot_data['EMA10'], color='#2962ff', width=1.0),
            mpf.make_addplot(plot_data['BOLL_UP'], color='#00897b', width=0.7),
            mpf.make_addplot(plot_data['BOLL_LOW'], color='#00897b', width=0.7),

            # MACD (Panel 2)
            mpf.make_addplot(plot_data['DIF'], panel=2, color='#2962ff', width=1.0, ylabel='MACD'),
            mpf.make_addplot(plot_data['DEA'], panel=2, color='#ff6d00', width=1.0),
            mpf.make_addplot(plot_data['MACD'], panel=2, type='bar', color='gray', alpha=0.5),

            # KDJ & RSI (Panel 3)
            mpf.make_addplot(plot_data['K'], panel=3, color='#2962ff', width=1.0, ylabel='Oscillator'),
            mpf.make_addplot(plot_data['D'], panel=3, color='#ff6d00', width=1.0),
            mpf.make_addplot(plot_data['J'], panel=3, color='#e91e63', width=1.0),
            mpf.make_addplot(plot_data['RSI'], panel=3, color='#9c27b0', width=1.0, linestyle='--'),
        ]

        # --- 绘图与获取 Axes 对象 ---
        buf = io.BytesIO()
        # returnfig=True 让我们可以操作 axes
        fig, axes = mpf.plot(
            plot_data,
            type='candle',
            style=s,
            addplot=add_plots,
            volume=True,
            datetime_format='%m-%d %H:%M',
            returnfig=True,
            panel_ratios=(5, 1, 1.5, 1.5),  # 主图:成交量:MACD:KDJ
            figsize=(10, 8),
            tight_layout=True,
            scale_padding={'top': 1}  # 顶部留白给文字
        )

        # --- 手动绘制 Legend (左上角数值) ---

        # 1. 主图 Legend (axes[0])
        # 价格颜色：涨绿跌红
        price_color = '#0ecb81' if last['close'] >= last['open'] else '#f6465d'

        # 第一行：标题 + 价格
        self._add_legend_text(axes[0], [
            (f"{title}", 'black'),
            (f"Price: {last['close']:.2f}", price_color)
        ], y_pos=0.96)

        # 第二行：均线和布林
        self._add_legend_text(axes[0], [
            (f"MA30:{last['MA30']:.2f}", '#ffbc00'),
            (f"EMA20:{last['EMA20']:.2f}", '#ab47bc'),
            (f"EMA10:{last['EMA10']:.2f}", '#2962ff'),
            (f"BOLL:{last['BOLL_UP']:.2f}/{last['BOLL_MID']:.2f}/{last['BOLL_LOW']:.2f}", '#00897b')
        ], y_pos=0.90)

        # 2. 成交量 Legend (axes[2]) - 注意 mplfinance 的 axes 索引可能因版本而异
        # 通常 axes[0]=Main, axes[1]=Secondary(if any), axes[2]=Volume(if panel=1)
        # 简单遍历寻找对应 label 的 axis 比较稳妥，或者按顺序写死
        vol_ax = axes[2]
        self._add_legend_text(vol_ax, [
            (f"VOL: {int(last['volume'])}", '#333333')
        ])

        # 3. MACD Legend (axes[4])
        macd_ax = axes[4]
        self._add_legend_text(macd_ax, [
            ("MACD", 'black'),
            (f"DIF:{last['DIF']:.2f}", '#2962ff'),
            (f"DEA:{last['DEA']:.2f}", '#ff6d00'),
            (f"HIST:{last['MACD']:.2f}", '#666666')
        ])

        # 4. KDJ & RSI Legend (axes[6])
        osc_ax = axes[6]
        self._add_legend_text(osc_ax, [
            (f"K:{last['K']:.2f}", '#2962ff'),
            (f"D:{last['D']:.2f}", '#ff6d00'),
            (f"J:{last['J']:.2f}", '#e91e63'),
            (f"RSI:{last['RSI']:.2f}", '#9c27b0')
        ])

        # 保存图片
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.1)
        plt.close(fig)

        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        return img_base64