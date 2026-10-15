from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "rag_private_test_corpus"
OUTPUT.mkdir(parents=True, exist_ok=True)

FONT_PATHS = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
]
FONT_PATH = next((path for path in FONT_PATHS if path.exists()), None)
if FONT_PATH is None:
    raise FileNotFoundError("未找到可用的中文字体")

pdfmetrics.registerFont(TTFont("CJK", str(FONT_PATH)))

PAGE_WIDTH, PAGE_HEIGHT = A4
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(
    name="CJKTitle",
    parent=styles["Title"],
    fontName="CJK",
    fontSize=23,
    leading=32,
    alignment=TA_CENTER,
    textColor=colors.HexColor("#173A63"),
    spaceAfter=12,
))
styles.add(ParagraphStyle(
    name="CJKSubtitle",
    parent=styles["Normal"],
    fontName="CJK",
    fontSize=12,
    leading=20,
    alignment=TA_CENTER,
    textColor=colors.HexColor("#52677F"),
))
styles.add(ParagraphStyle(
    name="CJKHeading",
    parent=styles["Heading1"],
    fontName="CJK",
    fontSize=18,
    leading=25,
    textColor=colors.HexColor("#173A63"),
    spaceAfter=12,
))
styles.add(ParagraphStyle(
    name="CJKBody",
    parent=styles["BodyText"],
    fontName="CJK",
    fontSize=10.5,
    leading=18,
    textColor=colors.HexColor("#27384A"),
    spaceAfter=8,
))
styles.add(ParagraphStyle(
    name="CJKSmall",
    parent=styles["BodyText"],
    fontName="CJK",
    fontSize=8.5,
    leading=13,
    textColor=colors.HexColor("#52677F"),
))


def p(text: str, style: str = "CJKBody") -> Paragraph:
    return Paragraph(text.replace("\n", "<br/>"), styles[style])


def table(rows: list[tuple[str, str]]) -> Table:
    data = [[p("字段", "CJKSmall"), p("内部定义", "CJKSmall")]]
    data.extend([[p(key, "CJKSmall"), p(value, "CJKSmall")] for key, value in rows])
    result = Table(data, colWidths=[44 * mm, 118 * mm], repeatRows=1)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173A63")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#EAF1F8")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#AAB9C8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return result


def cover(title: str, code: str, revision: str, purpose: str) -> list:
    return [
        Spacer(1, 45 * mm),
        p("云岬精密材料有限公司", "CJKSubtitle"),
        Spacer(1, 8 * mm),
        p(title, "CJKTitle"),
        Spacer(1, 12 * mm),
        table([
            ("项目代号", "北辰-47"),
            ("文档编号", code),
            ("当前版本", revision),
            ("资料属性", "虚构企业内部测试资料"),
            ("测试用途", purpose),
            ("互联网状态", "未公开、未发布、无法通过互联网检索"),
        ]),
        Spacer(1, 12 * mm),
        p("注意：本文全部组织、设备、协议、端口和事件均为虚构，仅用于验证 RAG 知识库检索，不得作为真实工业控制依据。", "CJKSmall"),
    ]


def header_footer(canvas, doc, short_title: str):
    canvas.saveState()
    canvas.setFont("CJK", 8)
    canvas.setFillColor(colors.HexColor("#66788B"))
    canvas.drawString(20 * mm, PAGE_HEIGHT - 12 * mm, f"北辰-47 | {short_title} | 内部测试")
    canvas.drawRightString(PAGE_WIDTH - 20 * mm, 10 * mm, f"第 {doc.page} 页")
    canvas.setStrokeColor(colors.HexColor("#D7E0E9"))
    canvas.line(20 * mm, PAGE_HEIGHT - 15 * mm, PAGE_WIDTH - 20 * mm, PAGE_HEIGHT - 15 * mm)
    canvas.restoreState()


def build_pdf(filename: str, short_title: str, pages: list[list]) -> Path:
    if len(pages) != 50:
        raise ValueError(f"{filename} 内容页定义必须恰好为 50，当前为 {len(pages)}")
    path = OUTPUT / filename
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=24 * mm,
        rightMargin=24 * mm,
        topMargin=22 * mm,
        bottomMargin=18 * mm,
        title=short_title,
        author="云岬精密材料有限公司（虚构）",
    )
    story = []
    for index, content in enumerate(pages):
        story.extend(content)
        if index < len(pages) - 1:
            story.append(PageBreak())
    callback = lambda canvas, current_doc: header_footer(canvas, current_doc, short_title)
    doc.build(story, onFirstPage=callback, onLaterPages=callback)
    actual_pages = len(PdfReader(str(path)).pages)
    if actual_pages != 50:
        raise RuntimeError(f"{filename} 生成后为 {actual_pages} 页，不是 50 页")
    return path


def heading_page(title: str, intro: str, rows: list[tuple[str, str]], notes: list[str]) -> list:
    content = [p(title, "CJKHeading"), p(intro), table(rows), Spacer(1, 5 * mm)]
    content.extend(p(f"• {note}") for note in notes)
    return content


def device_pages() -> list[list]:
    pages = [cover(
        "北辰-47 设备与协议技术规范",
        "BJ47-SPEC-R7-2049",
        "R7（有效）",
        "验证设备能力、协议参数、兼容条件和厂商映射检索",
    )]
    pages.extend([
        heading_page("1. 文档控制", "本规范是北辰-47 项目的设备能力基线。", [
            ("文档责任人", "沈砚，架构组 A-17"),
            ("批准识别码", "AZURE-CROWN-2049"),
            ("生效时间", "2026-06-18 08:30"),
            ("替代版本", "R6（已废止）"),
        ], ["现场例外规则优先于本规范默认参数。", "变更记录中的最新有效版本优先于本规范中的历史附注。"]),
        heading_page("2. 系统边界", "海雾基地由生产控制域、只读监测域和维护域组成。", [
            ("生产控制域", "ORBIT-CONTROL-47"),
            ("只读监测域", "ORBIT-RO-17"),
            ("维护域", "ORBIT-MAINT-29"),
            ("核心网关", "Aster-GW-9"),
        ], ["跨域写操作默认拒绝。", "协议名称均为本项目虚构名称。"]),
        heading_page("3. Aster-GW-9 启用条件", "Aster-GW-9 是北辰-47 的主边缘网关。", [
            ("制造商", "栖岳自动化"),
            ("主协议", "LumenLink-X7"),
            ("最低固件", "5.2.1"),
            ("允许板卡", "C7 或 C9"),
            ("温度条件", "不高于 44℃"),
            ("湿度条件", "未触发现场高湿例外"),
        ], ["所有条件同时满足时才启用主协议。", "安全等级 S3 会覆盖写权限，但不会单独改变协议。"]),
        heading_page("4. LumenLink-X7 参数", "LumenLink-X7 仅用于北辰-47 内部控制链路。", [
            ("TCP 端口", "31847"),
            ("心跳周期", "71 秒"),
            ("最大报文", "6151 字节"),
            ("字节序", "小端"),
            ("校验算法", "Orchid-CRC-29"),
            ("节点地址", "ASTER-C7-ORBIT"),
        ], ["连续丢失 3 个心跳后进入链路降级。", "控制写入需要双令牌 W47 和 K9。"]),
        heading_page("5. MorrowBus-Q2 兼容模式", "主协议条件不满足时使用 MorrowBus-Q2。", [
            ("默认 TCP 端口", "28611"),
            ("默认心跳周期", "43 秒"),
            ("最大报文", "3079 字节"),
            ("字节序", "大端"),
            ("校验算法", "Quartz-Sum-17"),
            ("兼容地址", "MORROW-Q2-FALLBACK"),
        ], ["现场高湿模式会覆盖端口、心跳和最大报文。", "字节序仍采用本页定义的大端。"]),
        heading_page("6. OPC UA 诊断接口", "网关提供标准 OPC UA 诊断接口，但不承担生产控制。", [
            ("允许操作", "状态读取、固件查询、证书到期查询"),
            ("禁止操作", "生产参数写入、设备启停、配方下发"),
            ("诊断端点", "opc.tcp://aster-gw9:49321"),
            ("安全策略", "Basic256Sha256-SignAndEncrypt"),
        ], ["OPC UA 可在 S3 状态下继续只读访问。", "诊断接口不可替代 LumenLink-X7 或 MorrowBus-Q2。"]),
        heading_page("7. 厂商与设备映射", "以下映射用于采购和兼容性检查。", [
            ("栖岳自动化", "Aster-GW-9、Aster-IO-4、Nimbus-Relay-7"),
            ("澄屿控制", "Mica-PLC-6、Mica-Drive-3"),
            ("岚桥仪表", "Vela-Sensor-2、Vela-Meter-8"),
            ("弦月网络", "Orion-SW-5、Orion-FW-2"),
        ], ["Aster-GW-8 不属于北辰-47 采购清单。", "厂商映射不能推导具体现场启用协议。"]),
        heading_page("8. 固件兼容矩阵", "固件版本决定主协议功能是否可用。", [
            ("5.1.x", "仅 MorrowBus-Q2"),
            ("5.2.0", "LumenLink-X7 试验版，不允许生产"),
            ("5.2.1 - 5.3.4", "LumenLink-X7 正式支持"),
            ("5.3.5", "暂停部署，证书槽缺陷"),
            ("5.3.6 及以上", "LumenLink-X7 正式支持，需 R9 证书包"),
        ], ["版本比较按语义版本逐段比较。", "5.3.5 的禁止规则优先于一般最低版本规则。"]),
        heading_page("9. 地址和证书命名", "节点命名用于区分生产、监测和维护身份。", [
            ("生产证书前缀", "CERT-ORBIT-P"),
            ("只读证书前缀", "CERT-ORBIT-R"),
            ("维护证书前缀", "CERT-ORBIT-M"),
            ("证书轮换周期", "137 天"),
            ("时间同步容差", "± 370 毫秒"),
        ], ["超过时间同步容差会拒绝写请求。", "证书槽编号见各设备档案。"]),
    ])

    vendors = ["栖岳自动化", "澄屿控制", "岚桥仪表", "弦月网络"]
    protocols = ["LumenLink-X7", "MorrowBus-Q2", "SlateMesh-A4", "VioletWire-P6"]
    for index in range(1, 41):
        device_id = f"DEV-{index:03d}"
        model = f"Nimbus-{(index * 7) % 41 + 10:02d}"
        vendor = vendors[(index - 1) % len(vendors)]
        protocol = protocols[(index - 1) % len(protocols)]
        port = 30000 + index * 17
        cold_start = 20 + (index * 3) % 41
        cert_slot = f"CERT-P{index:02d}-{(index * 29) % 997:03d}"
        pages.append(heading_page(
            f"{9 + index}. 设备档案 {device_id}",
            f"{device_id} 是北辰-47 资产台账中的虚构设备档案，用于验证精确字段检索。",
            [
                ("设备型号", model),
                ("制造商", vendor),
                ("首选协议", protocol),
                ("服务端口", str(port)),
                ("冷启动等待", f"{cold_start} 秒"),
                ("证书槽", cert_slot),
                ("资产标签", f"ORBIT-ASSET-{index:02d}-{index * 113:04d}"),
                ("校准周期", f"{90 + index * 2} 天"),
            ],
            [
                f"档案校验短语为 GLASS-{index:02d}-COMET-{(index * 83) % 10000:04d}。",
                f"该设备只能与分区 HZ-Z{index:02d} 的同号控制柜建立默认绑定。",
                "出现现场例外时，必须先应用运行规则，再应用本档案默认参数。",
            ],
        ))
    return pages


def operations_pages() -> list[list]:
    pages = [cover(
        "海雾基地运行与安全例外规则",
        "HZ-OPS-R4-731",
        "R4（有效）",
        "验证现场条件、权限优先级、维护窗口和分区规则检索",
    )]
    pages.extend([
        heading_page("1. 文档控制", "本规则覆盖海雾基地所有生产分区。", [
            ("规则责任人", "林汐，运行保障组 O-31"),
            ("批准识别码", "SILVER-TIDE-731"),
            ("生效时间", "2026-07-02 06:00"),
            ("上位文件", "HZ-SAFE-CORE-R3"),
        ], ["安全规则优先于设备默认配置。", "同等级冲突按变更记录中的最新有效版本处理。"]),
        heading_page("2. 规则优先级", "参数冲突时必须按以下顺序决策。", [
            ("优先级 1", "人员安全与 S3 锁定"),
            ("优先级 2", "现场环境例外"),
            ("优先级 3", "最新有效变更单"),
            ("优先级 4", "设备技术规范"),
            ("优先级 5", "设备档案默认值"),
        ], ["高优先级只覆盖明确列出的字段。", "未被覆盖的字段继续继承低优先级定义。"]),
        heading_page("3. 北区四号线高湿规则", "北区四号线编号 HZ-L4，湿度条件使用连续窗口判断。", [
            ("触发条件", "相对湿度高于 83%，持续至少 180 秒"),
            ("强制协议", "MorrowBus-Q2"),
            ("临时端口", "28639"),
            ("心跳周期", "31 秒"),
            ("最大报文", "2087 字节"),
            ("告警标识", "MIST-FALCON-7314"),
        ], ["字节序继承 MorrowBus-Q2 的大端定义。", "湿度恢复到 78% 以下持续 300 秒后才解除。"]),
        heading_page("4. 高温规则", "设备柜温度超过阈值时执行降级。", [
            ("触发条件", "温度高于 44℃，持续 90 秒"),
            ("强制协议", "MorrowBus-Q2"),
            ("端口", "28627"),
            ("心跳周期", "37 秒"),
            ("告警标识", "EMBER-WREN-4427"),
        ], ["同时触发高温和高湿时，采用高湿端口 28639。", "最大报文采用更严格的 2087 字节。"]),
        heading_page("5. S3 安全锁定", "S3 用于人员进入危险区域或联锁异常。", [
            ("生产参数写入", "禁止"),
            ("状态读取", "允许"),
            ("报警确认", "允许，仅限 ACK-3 指令"),
            ("只读通道", "RO-AXIS-9407"),
            ("解除方式", "双人复核并提交 SAFE-UNLOCK-31"),
        ], ["S3 写禁止优先于高湿模式的任何写许可。", "S3 不改变已经由环境规则确定的协议和端口。"]),
        heading_page("6. S2 受控状态", "S2 允许受控生产操作。", [
            ("状态读取", "允许"),
            ("生产写入", "允许，需 W47 与 K9 双令牌"),
            ("批量写入", "每批不超过 19 个点位"),
            ("会话时限", "17 分钟"),
        ], ["环境规则仍可能改变协议参数。", "超过会话时限后必须重新认证。"]),
        heading_page("7. 计划维护窗口", "维护窗口按基地本地时间计算。", [
            ("周期", "每月第二个星期四"),
            ("开始时间", "01:37"),
            ("结束时间", "02:22"),
            ("维护识别码", "INDIGO-PIER-6629"),
            ("延期条件", "雷暴等级达到 T4"),
        ], ["延期后顺延至下一周星期四同一时段。", "维护窗口不自动解除 S3。"]),
        heading_page("8. 异常恢复顺序", "多重异常解除后按固定顺序恢复。", [
            ("步骤 1", "确认湿度低于 78% 持续 300 秒"),
            ("步骤 2", "确认温度低于 40℃ 持续 180 秒"),
            ("步骤 3", "检查 Orchid-CRC-29 连续 7 帧通过"),
            ("步骤 4", "切回 LumenLink-X7 并等待 2 个心跳"),
            ("步骤 5", "恢复受控写权限"),
        ], ["任何一步失败都返回 MorrowBus-Q2。", "恢复记录号前缀为 RECOVER-HZ4。"]),
        heading_page("9. 审计字段", "所有自动切换必须写入不可变审计记录。", [
            ("必要字段", "设备、原协议、新协议、触发规则、时间、操作人"),
            ("哈希算法", "MistHash-41"),
            ("保留周期", "913 天"),
            ("审计时钟", "CLOCK-ORBIT-7"),
        ], ["缺少触发规则编号的记录视为无效。", "审计记录不得由生产控制账户删除。"]),
    ])

    risk_codes = ["低", "中", "高", "受控"]
    protocols = ["LumenLink-X7", "MorrowBus-Q2", "SlateMesh-A4", "VioletWire-P6"]
    for index in range(1, 41):
        humidity = 60 + index % 27
        temperature = 31 + index % 17
        dwell = 90 + index * 7
        token = f"ZONE-{index:02d}-TOKEN-{(index * 137) % 10000:04d}"
        pages.append(heading_page(
            f"{9 + index}. 分区运行卡 HZ-Z{index:02d}",
            f"HZ-Z{index:02d} 是海雾基地虚构运行分区，本页参数仅适用于该分区。",
            [
                ("默认协议", protocols[(index - 1) % len(protocols)]),
                ("湿度预警值", f"{humidity}%"),
                ("温度预警值", f"{temperature}℃"),
                ("判定持续时间", f"{dwell} 秒"),
                ("风险等级", risk_codes[(index - 1) % len(risk_codes)]),
                ("现场令牌", token),
                ("值班组", f"SHIFT-{chr(65 + index % 5)}-{index:02d}"),
                ("撤销确认数", str(2 + index % 4)),
            ],
            [
                f"该分区绑定设备档案 DEV-{index:03d}。",
                f"分区验证短语为 HARBOR-{(index * 59) % 1000:03d}-LANTERN-{index:02d}。",
                "若本页阈值与北区四号线专用规则冲突，专用规则优先。",
            ],
        ))
    return pages


def change_pages() -> list[list]:
    pages = [cover(
        "北辰-47 变更记录与故障案例",
        "BJ47-CHANGE-R9-6207",
        "R9（有效）",
        "验证版本冲突、最新规则、故障根因和恢复步骤检索",
    )]
    pages.extend([
        heading_page("1. 文档控制", "本册记录协议参数变更和已关闭故障。", [
            ("变更经理", "顾澜，配置管理组 C-09"),
            ("批准识别码", "COPPER-REEF-6207"),
            ("当前基线", "R9"),
            ("基线日期", "2026-07-19"),
        ], ["只有状态为有效且日期最新的变更可以覆盖旧值。", "故障案例不能自动成为配置规则。"]),
        heading_page("2. 版本索引", "历史版本保留用于测试冲突消解。", [
            ("R6", "2025-12-11，已废止"),
            ("R7", "2026-03-08，已废止"),
            ("R8", "2026-05-26，已废止"),
            ("R9", "2026-07-19，有效"),
            ("R10 草案", "未批准，不得使用"),
        ], ["草案版本即使编号更高也不能覆盖有效版本。", "回答参数时必须同时检查状态和日期。"]),
        heading_page("3. R7 历史参数", "R7 曾定义北区四号线高湿参数，现已废止。", [
            ("端口", "28617"),
            ("心跳周期", "45 秒"),
            ("最大报文", "3001 字节"),
            ("状态", "已废止"),
        ], ["不得用于当前生产决策。", "保留本页用于验证检索系统能否识别旧版本干扰。"]),
        heading_page("4. R8 历史参数", "R8 调整过同一组参数，现已废止。", [
            ("端口", "28631"),
            ("心跳周期", "35 秒"),
            ("最大报文", "2303 字节"),
            ("状态", "已废止"),
        ], ["R8 被 R9 完整替代。", "不得因为相似度较高而优先选择 R8。"]),
        heading_page("5. R9 有效变更 CR-ORBIT-6207", "R9 是当前批准的高湿参数基线。", [
            ("端口", "28639"),
            ("心跳周期", "31 秒"),
            ("最大报文", "2087 字节"),
            ("告警标识", "MIST-FALCON-7314"),
            ("状态", "有效"),
        ], ["本变更与运行规则 R4 一致。", "生效后所有新会话必须使用 R9 参数。"]),
        heading_page("6. R10 未批准草案", "R10 仅是实验室建议，不属于当前基线。", [
            ("建议端口", "28651"),
            ("建议心跳", "23 秒"),
            ("状态", "未批准草案"),
            ("否决原因", "交换机缓存压力超限"),
        ], ["严禁在回答当前有效参数时采用本页数值。", "未来是否启用需新的批准记录。"]),
        heading_page("7. 冲突判定算法", "配置冲突必须按四步法处理。", [
            ("第一步", "过滤未批准和已废止记录"),
            ("第二步", "比较适用基地与生产线"),
            ("第三步", "选择生效日期最新记录"),
            ("第四步", "应用安全和现场例外优先级"),
        ], ["不能仅按版本号大小选择。", "每次决策必须输出所用文档编号。"]),
        heading_page("8. 已知误用模式", "以下错误是知识库问答重点检查项。", [
            ("误用 A", "选中 R8 的端口 28631"),
            ("误用 B", "选中 R10 草案端口 28651"),
            ("误用 C", "忽略 S3 写禁止"),
            ("误用 D", "把 OPC UA 诊断接口用于生产控制"),
        ], ["出现任一误用即判定复杂场景测试失败。", "正确回答应解释排除旧值的原因。"]),
        heading_page("9. 回归验证要求", "R9 上线后必须完成五项验证。", [
            ("链路验证", "连续 11 个心跳成功"),
            ("权限验证", "S3 下写请求全部拒绝"),
            ("审计验证", "MistHash-41 校验通过"),
            ("恢复验证", "按 RECOVER-HZ4 顺序完成"),
            ("签收口令", "TEAL-ANCHOR-9917"),
        ], ["五项全部通过才能关闭变更。", "验证结果保留 913 天。"]),
    ])

    causes = ["证书时钟偏移", "湿度传感器漂移", "交换机队列拥塞", "固件槽位不一致", "校验算法配置错误"]
    symptoms = ["间歇掉线", "写请求拒绝", "心跳抖动", "状态值冻结", "告警重复"]
    for index in range(1, 41):
        incident_id = f"INC-{index:03d}"
        device_id = f"DEV-{index:03d}"
        root_code = f"ROOT-{(index * 47) % 997:03d}-{chr(65 + index % 6)}"
        token = f"CLOSE-{index:02d}-{(index * 211) % 10000:04d}"
        pages.append(heading_page(
            f"{9 + index}. 故障案例 {incident_id}",
            f"{incident_id} 是已关闭的虚构故障案例，用于测试精确事件检索和因果区分。",
            [
                ("关联设备", device_id),
                ("发生分区", f"HZ-Z{index:02d}"),
                ("主要现象", symptoms[(index - 1) % len(symptoms)]),
                ("确认根因", causes[(index - 1) % len(causes)]),
                ("根因代码", root_code),
                ("恢复动作", f"执行序列 RCV-{index:02d}-B 后等待 {25 + index} 秒"),
                ("关闭口令", token),
                ("复发观察期", f"{48 + index * 3} 小时"),
            ],
            [
                f"取证校验短语为 EVIDENCE-{(index * 97) % 10000:04d}-NORTH。",
                "案例参数只描述该次事件，不覆盖有效配置基线。",
                f"关闭审批组为 REVIEW-{chr(65 + index % 4)}-{index:02d}。",
            ],
        ))
    return pages


def write_answer_key(paths: list[Path]) -> Path:
    answer_path = OUTPUT / "测试问题与标准答案.md"
    content = f"""# 北辰-47 私有知识库测试说明

## 资料清单

- `{paths[0].name}`：50 页，设备能力、协议参数和 40 个设备档案。
- `{paths[1].name}`：50 页，现场例外、安全权限、维护窗口和 40 个分区运行卡。
- `{paths[2].name}`：50 页，版本冲突、有效变更和 40 个故障案例。

这些资料完全虚构，不存在于互联网。先将三份 PDF 上传到同一个 RAGFlow 知识库，解析成功后再开始测试。每次独立测试建议新建对话。

## 核心复杂问题

### 问题 1：跨三文档条件推理

> 海雾基地北区四号线的一台 Aster-GW-9，板卡 C7，固件 5.2.3，温度 39℃，相对湿度 86% 已持续 200 秒，当前安全等级 S3。请给出最终协议、端口、心跳周期、最大报文、字节序、写权限和只读通道，并说明为什么没有采用其他参数。

标准答案：

- 协议：MorrowBus-Q2。
- 端口：28639。
- 心跳：31 秒。
- 最大报文：2087 字节。
- 字节序：大端。
- 写权限：禁止。
- 只读通道：RO-AXIS-9407。
- 原因：高湿超过 83% 且持续超过 180 秒，触发北区四号线高湿规则；S3 禁止写入且优先级最高；字节序未被现场规则覆盖，因此继承 MorrowBus-Q2 的大端定义。R7 的 28617、R8 的 28631 已废止，R10 的 28651 未批准，均不能使用。

预期引用：三份 PDF 都应被引用。

### 问题 2：条件解除后的重新决策

> 延续上一个场景，湿度下降到 76% 并持续 320 秒，温度 38℃并持续 180 秒，Orchid-CRC-29 已连续 7 帧通过，恢复步骤均已完成，安全等级改为 S2。最终应使用什么协议和参数，能否写入？

标准答案：LumenLink-X7；端口 31847；心跳 71 秒；最大报文 6151 字节；小端；S2 下持有 W47 与 K9 双令牌时允许受控写入。解除高湿要求低于 78% 持续 300 秒，本题满足。

### 问题 3：版本冲突

> 有四个人分别说北区四号线高湿端口是 28617、28631、28639 和 28651。哪个是当前有效值？逐一解释其他三个为什么不能用。

标准答案：28639。28617 属于已废止 R7；28631 属于已废止 R8；28651 属于未批准 R10 草案。

### 问题 4：标准协议边界

> Aster-GW-9 能否只通过 OPC UA 完成生产参数写入和设备启停？如果只查询固件与证书到期信息呢？

标准答案：不能用 OPC UA 做生产参数写入或设备启停；可以用它查询固件、证书到期和状态，端点是 `opc.tcp://aster-gw9:49321`。

### 问题 5：计划维护

> 海雾基地正常计划维护在什么时候？维护识别码是什么？如果当天雷暴达到 T4 怎么处理？

标准答案：每月第二个星期四 01:37 至 02:22；识别码 INDIGO-PIER-6629；T4 时顺延到下一周星期四同一时段。

## 精确定位测试

### 问题 6

> DEV-023 的型号、厂商、服务端口、冷启动等待、证书槽和档案校验短语分别是什么？

标准答案：Nimbus-48；岚桥仪表；30391；48 秒；CERT-P23-667；GLASS-23-COMET-1909。

### 问题 7

> HZ-Z17 的默认协议、湿度预警、温度预警、判定持续时间、现场令牌和验证短语是什么？

标准答案：LumenLink-X7；77%；31℃；209 秒；ZONE-17-TOKEN-2329；HARBOR-003-LANTERN-17。

### 问题 8

> INC-023 关联哪个设备和分区？主要现象、确认根因、根因代码、关闭口令和观察期是什么？

标准答案：DEV-023；HZ-Z23；心跳抖动；交换机队列拥塞；ROOT-084-F；CLOSE-23-4853；117 小时。

## 抗幻觉测试

### 问题 9

> 北辰-47 的 AuroraMax-Z99 控制器支持什么协议？

标准答案：知识库没有 AuroraMax-Z99 信息，不能编造。

### 问题 10

> R10 已经批准了吗？请给出批准人和正式生效时间。

标准答案：R10 未批准，知识库没有批准人和正式生效时间，不能编造。

## 多轮上下文测试

依次提问：

1. “介绍 DEV-023 的通信参数。”
2. “它绑定哪个分区？”
3. “那个分区的现场令牌是什么？”
4. “如果我说的是它对应的故障案例，根因又是什么？”

预期：系统持续理解“它”和“那个分区”，最终关联 DEV-023、HZ-Z23 和 INC-023。

## 评分建议

| 项目 | 分值 | 判定标准 |
|---|---:|---|
| 是否调用 RAGFlow | 20 | 前端调用链明确显示 RAGFlow 已调用 |
| 关键参数准确率 | 30 | 数字、代号和权限无错误 |
| 跨文档推理 | 20 | 能综合设备、运行规则和变更记录 |
| 版本冲突处理 | 10 | 排除废止版本和未批准草案 |
| 引用正确性 | 10 | 引用确实包含结论的文档 |
| 抗幻觉能力 | 10 | 资料缺失时明确说明，不编造 |

总分 85 分以上，可认为基础 RAG 链路达到可演示水平。

## RAGFlow 导入建议

- 三份 PDF 放入同一个新建知识库，不要混入公开资料。
- Parser 选择 General，第一轮 PDF parser 选择 Naive。
- Recommended chunk size 建议 256。
- Overlapped percent 建议 10%。
- Image & table context window 建议 64。
- PageIndex 第一轮关闭；当前 DeepAgent 检索请求尚未启用 `toc_enhance`。
- 文档均带可检索文字层，不需要 OCR 才能提取内容。
- 等三份文档全部显示 Success 后，先在 Retrieval testing 中搜索 `MIST-FALCON-7314` 和 `ROOT-084-F`。
- DeepAgent 测试时观察调用链，必须出现“RAGFlow 已调用”。
- 完整参数原因参见项目根目录的 `RAGFlow第一轮解析参数配置说明.md`。
"""
    answer_path.write_text(content, encoding="utf-8")
    return answer_path


def main() -> None:
    paths = [
        build_pdf("01_北辰47设备与协议技术规范_R7.pdf", "设备与协议技术规范", device_pages()),
        build_pdf("02_海雾基地运行与安全例外规则_R4.pdf", "运行与安全例外规则", operations_pages()),
        build_pdf("03_北辰47变更记录与故障案例_R9.pdf", "变更记录与故障案例", change_pages()),
    ]
    answer_key = write_answer_key(paths)
    for path in [*paths, answer_key]:
        print(path)


if __name__ == "__main__":
    main()
