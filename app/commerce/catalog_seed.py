"""Fictional catalog fixtures; no external merchant data."""

EXTRA_PRODUCTS = [
    ("P101", "静听降噪耳机", "electronics", 39900, "支持主动降噪，续航 8 小时，黑白灰三色。", "降噪耳机"),
    ("P102", "便携蓝牙音箱", "electronics", 15900, "蓝牙连接，续航 10 小时，USB-C 充电。", "音箱"),
    ("P103", "轻薄移动电源", "electronics", 12900, "容量 10000mAh，支持 20W 有线快充。", "移动电源"),
    ("P104", "无线办公鼠标", "electronics", 6900, "2.4GHz 接收器连接，使用 AA 电池。", "鼠标"),
    ("P105", "机械键盘", "electronics", 25900, "有线 USB 连接，87 键布局，红轴。", "键盘"),
    ("P106", "桌面阅读灯", "electronics", 11900, "三档色温，亮度可调，插电使用。", "台灯"),
    ("P201", "连帽卫衣", "clothing", 16900, "棉混纺面料，M/L/XL，建议反面轻柔洗涤。", "卫衣"),
    ("P202", "直筒休闲裤", "clothing", 13900, "弹力腰围，M/L/XL，常规直筒版型。", "休闲裤"),
    ("P203", "轻薄防风外套", "clothing", 22900, "聚酯纤维面料，M/L/XL，不承诺专业防水。", "外套"),
    ("P204", "基础短袜三双装", "clothing", 2900, "棉混纺，均码，三双一组。", "袜子"),
    ("P205", "针织围巾", "clothing", 8900, "腈纶混纺面料，建议手洗平铺晾干。", "围巾"),
    ("P301", "定制帆布袋", "custom", 5900, "支持单面文字印制，制作前核对内容。", "帆布袋"),
    ("P302", "定制笔记本", "custom", 4900, "A5 尺寸，支持封面刻字，80 页。", "笔记本"),
    ("P303", "定制钥匙扣", "custom", 3900, "金属材质，支持单面刻字。", "钥匙扣"),
    ("P304", "定制纪念相框", "custom", 7900, "适配六寸照片，支持边框刻字。", "相框"),
]


def extend_catalog(products):
    for index, (pid, name, category, price, description, keyword) in enumerate(EXTRA_PRODUCTS):
        products[pid] = dict(product_id=pid, name=name, category=category,
            price=f"{price / 100:.2f}", price_cents=price, stock=(0 if index == 3 else 15 + index * 3),
            customized=category == "custom", description=description, keywords=[keyword, pid])
