// 生成《产线投影面积、公摊工时与运费估算——计算逻辑说明》Word 文档
// 用法：npm install && node make_doc.js ../../webapp/static/docs/calculation-logic.zh.docx
// 文中数字为 101–104 四张 DXF 在 config.yaml 默认参数下的结果（estimate.py 计算）；公式或默认参数变了要同步改这里
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, Header, Footer,
  AlignmentType, HeadingLevel, WidthType, ShadingType, BorderStyle, LevelFormat, PageNumber,
  VerticalAlign,
} = require("docx");

const OUT = process.argv[2];
const FONT = { ascii: "Arial", hAnsi: "Arial", eastAsia: "Microsoft YaHei", cs: "Arial" };
const MONO = { ascii: "Consolas", hAnsi: "Consolas", eastAsia: "Microsoft YaHei", cs: "Consolas" };
const TEAL = "1F4E5A", ACCENT = "2A8C8C", GREY = "6B7780", RULE = "BFC7CC";
const CONTENT_W = 9638;                      // A4 宽 11906 − 左右页边距 2 × 1134

// ---------- 小工具 ----------
const runs = (parts, base = {}) => (Array.isArray(parts) ? parts : [parts]).map((x) =>
  typeof x === "string" ? new TextRun({ text: x, ...base }) : new TextRun({ ...base, text: x.t, bold: x.b, color: x.c }));

const para = (parts, opts = {}) => new Paragraph({ spacing: { after: 120, line: 320 }, ...opts, children: runs(parts) });
const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const bullet = (parts) => new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 80, line: 300 }, children: runs(parts) });
let listInstance = 0;
const steps = (items) => {
  listInstance += 1;
  return items.map((parts) => new Paragraph({ numbering: { reference: "steps", level: 0, instance: listInstance },
                                              spacing: { after: 80, line: 300 }, children: runs(parts) }));
};

// 公式框：浅灰底、左侧青色竖线、等宽字体
const formula = (lines) => lines.map((line, i) => new Paragraph({
  shading: { type: ShadingType.CLEAR, color: "auto", fill: "F2F5F6" },
  border: { left: { style: BorderStyle.SINGLE, size: 18, color: ACCENT, space: 10 } },
  indent: { left: 200, right: 120 },
  spacing: { before: i === 0 ? 80 : 0, after: i === lines.length - 1 ? 160 : 0, line: 300 },
  children: runs(line, { font: MONO, size: 19 }),
}));

// 提示框：浅黄底
const callout = (parts) => new Paragraph({
  shading: { type: ShadingType.CLEAR, color: "auto", fill: "FFF6DD" },
  border: { left: { style: BorderStyle.SINGLE, size: 18, color: "D9A21B", space: 10 } },
  indent: { left: 200, right: 120 }, spacing: { before: 80, after: 200, line: 320 },
  children: runs(parts),
});

const cellBorder = { style: BorderStyle.SINGLE, size: 4, color: RULE };
function table(headers, rows, widths, { numeric = [], totalRow = false } = {}) {
  const sum = widths.reduce((a, b) => a + b, 0);
  if (sum !== CONTENT_W) throw new Error(`列宽之和 ${sum} ≠ ${CONTENT_W}`);
  const cell = (text, w, { head = false, bold = false, right = false, fill } = {}) => new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, color: "auto", fill } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ alignment: right ? AlignmentType.RIGHT : AlignmentType.LEFT,
                               children: runs(text, { size: 19, bold: head || bold, color: head ? "FFFFFF" : undefined }) })],
  });
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: widths,
    borders: { top: cellBorder, bottom: cellBorder, left: cellBorder, right: cellBorder,
               insideHorizontal: cellBorder, insideVertical: cellBorder },
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) =>
        cell(h, widths[i], { head: true, fill: TEAL, right: numeric.includes(i) })) }),
      ...rows.map((r, ri) => {
        const isTotal = totalRow && ri === rows.length - 1;
        return new TableRow({ children: r.map((v, i) => cell(v, widths[i], {
          bold: isTotal, right: numeric.includes(i), fill: isTotal ? "E6EEF0" : (ri % 2 ? "FAFBFB" : undefined) })) });
      }),
    ],
  });
}
const gap = () => new Paragraph({ spacing: { after: 120 }, children: [] });
const caption = (t) => new Paragraph({ spacing: { before: 120, after: 80 }, keepNext: true,
                                       children: [new TextRun({ text: t, bold: true, size: 19, color: TEAL })] });
const note = (t) => new Paragraph({ spacing: { before: 60, after: 160 }, children: [new TextRun({ text: t, size: 17, color: GREY })] });

// ---------- 正文 ----------
const body = [
  new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: "产线投影面积、公摊工时与运费估算", bold: true, size: 40, color: TEAL })] }),
  new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: "计算逻辑说明", bold: true, size: 30, color: ACCENT })] }),
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({ text: "适用版本：layout-estimator（2026-11-07：框架箱运价直接输入，去掉面积误差系数）", size: 18, color: GREY })] }),
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({ text: "示例数据：101–104 四张 DXF 产线布局图，默认参数（config.yaml）", size: 18, color: GREY })] }),
  new Paragraph({ spacing: { after: 240 }, border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE, space: 8 } },
                  children: [new TextRun({ text: "日期：2026-11-07", size: 18, color: GREY })] }),

  h1("一、结论摘要"),
  para("本工具从产线布局图（DXF 或 PDF）测量两种面积，再用业务参数分别换算出公摊工时、40HQ 装柜数量和海运费。四张 DXF 合计结果如下："),
  table(["指标", "结果", "说明"], [
    ["精细面积（设备投影）", "3,559.7 m²", "只计设备外轮廓内的面积，不含通道、空地"],
    ["基准面积（总长 × 总宽）", "10,078.0 m²", "各条线体外包矩形面积之和，即工时表现行口径"],
    ["公摊工时", "10,481 小时", "钳工 5,240.6 h + 电工 5,240.6 h"],
    ["40HQ 箱数", "225 箱（中）", "范围 196–265 箱，其中框架箱 22 箱"],
    ["海运费", "US$1,324,759（中）", "范围 US$1,155,753 – US$1,562,626；上海 → 鹿特丹"],
  ], [2700, 2300, 4638]),
  gap(),
  callout([{ t: "注意：", b: true }, "海运费是本工具输出的唯一金额。公摊工时只给出小时数——工具中没有人工费率，不会把工时换算成人工费用。"]),

  h1("二、计算流程总览"),
  para("两种面积分开计算、分开使用，从不混用或相加："),
  table(["面积口径", "如何得到", "用于", "最终输出"], [
    ["精细面积（设备投影面积）", "所有设备外轮廓取并集", "包装 → 装箱 → 运费", "40HQ 箱数、海运费（US$）"],
    ["基准面积（总长 × 总宽）", "每条线体的外包矩形相加", "公摊工时", "钳工、电工工时（小时）"],
  ], [2500, 2600, 2000, 2538]),
  gap(),
  ...formula([
    "产线布局图（DXF）",
    " ├─ 精细面积 → 包装后面积 → 40HQ 箱数 → 海运费（US$）",
    " └─ 基准面积 → 钳工工时 + 电工工时 → 公摊工时（小时）",
  ]),

  h1("三、第一步：从图纸测量面积"),
  para("面积由确定性的几何计算得出，不由 AI 估算；同一文件、同一参数，每次结果相同。"),
  h2("3.1 单位"),
  bullet("优先读取 DXF 文件头 $INSUNITS 声明的单位。"),
  bullet("文件未声明单位时，使用同一文件（按 SHA-256 哈希识别）已审核的单位记录，或由用户在上传时选择。"),
  bullet("以上都没有时，停止计算并提示补充单位；绝不默认按毫米处理。"),
  h2("3.2 精细面积（设备投影面积）"),
  ...steps([
    "逐个读取模型空间中的图块插入（INSERT），分类为：设备 / 排除（标注、背景框、空图块、线外物体等）/ 移动设备 / 待定。",
    "每个设备图块在自身坐标中求一次外轮廓：圆弧、样条等曲线离散为折线，闭合绘图中的微小缺口，填平设备内部细节，只保留外边界。",
    "同一图块的多次插入共用这一外轮廓，再按各自的平移、旋转、镜像、缩放放到图上。",
    "所有设备外轮廓取并集：相互重叠的部分只算一次，设备之间的空地和通道不计入。",
    "文字、尺寸标注、引线、图框、人员符号等注释图形不参与面积；移动设备和待定物体单独列出，不计入精细面积。",
  ]),
  para([{ t: "说明：", b: true }, "DXF 目前还不能拆分工位，精细面积取整张图的设备投影面积。"]),
  h2("3.3 基准面积（产线总长 × 总宽）"),
  ...steps([
    "找出与产线长边平行、贯穿整张图、宽度 ≥ 3 m 的空通道，用它把设备分成若干条线体（窄于 3 m 的视为操作走道，不分线）。",
    "每条线体取其设备（面积 ≥ 2 m² 的设备块）的外包矩形，长、宽各取到 0.1 m。",
    "各线体的 总长 × 总宽 相加，线体之间的通道不计入。",
  ]),
  para([{ t: "依据：", b: true }, "用户确认（2026-11-02），工时表“公摊&投影面积”一栏人工录入的就是产线总长 × 宽。"]),

  h1("四、第二步：公摊工时（按基准面积）"),
  ...formula([
    "钳工工时 = 基准面积 × 0.52 小时/m²",
    "电工工时 = 基准面积 × 0.52 小时/m²",
    "公摊工时 = 钳工工时 + 电工工时 = 基准面积 × 1.04",
  ]),
  para("系数来源：现有《钳工/电工工时推算》表中，钳工、电工各有一行“公摊&投影面积”，系数均为 0.52 小时/m²。面积口径可在参数中切换为“精细”（按设备投影面积计算）。"),

  h1("五、第三步：40HQ 装柜数量（按精细面积）"),
  ...formula([
    "包装后面积   = 精细面积 × 1.15（包装系数）",
    "40HQ 内底面积 = 12.03 m × 2.35 m = 28.27 m²",
    "可用底面积   = 28.27 m² × 地板利用率（设备不可堆叠、形状不规则）",
    "",
    "低（乐观）= ⌈ 精细面积 × 1.15 ÷ (28.27 × 0.75) ⌉",
    "中        = ⌈ 精细面积 × 1.15 ÷ (28.27 × 0.65) ⌉",
    "高（保守）= ⌈ 精细面积 × 1.15 ÷ (28.27 × 0.55) ⌉",
    "",
    "框架箱数（中）= 四舍五入（中箱数 × 10%）",
  ]),
  bullet("⌈ ⌉ 表示向上取整到整箱。"),
  bullet("0.65 是保守（0.55）与乐观（0.75）地板利用率的平均值。"),
  bullet("低、中、高三种情形只差在地板利用率，面积和包装系数相同。"),
  bullet("框架箱 / 开顶箱用于超宽（> 2.35 m）或超高（> 2.6 m）的设备，默认占 10%。"),

  h1("六、第四步：海运费"),
  ...formula([
    "普通 40HQ 每箱 = 运价 × (1 + 保险比例) + 杂费",
    "              = 3,997 × 1 + 1,500 = US$5,497",
    "框架箱每箱     = 框架箱海运价 × (1 + 保险比例) + 杂费",
    "              = 7,994 × 1 + 1,500 = US$9,494",
    "",
    "运费（中）= (中箱数 − 框架箱数) × 5,497 + 框架箱数 × 9,494",
    "",
    "加权箱价  = 90% × 5,497 + 10% × 9,494 = US$5,896.70",
    "运费（低）= 低箱数 × 5,896.70",
    "运费（高）= 高箱数 × 5,896.70",
  ]),
  para("40HQ 运价为上海 → 鹿特丹 40 尺现货价 US$3,997（Drewry WCI，不区分 GP / HQ）。框架箱 / 开顶箱海运价按航线直接输入，默认 US$7,994 是占位值（= 40HQ 运价 × 2），报价前需换成货代报价。杂费为起运港 + 目的港杂费、报关、短驳。"),

  h1("七、算例：101 线"),
  table(["步骤", "计算", "结果"], [
    ["精细面积", "图纸测量", "199.7 m²"],
    ["基准面积", "72.1 m × 6.9 m", "497.5 m²"],
    ["钳工工时", "497.5 × 0.52", "258.7 h"],
    ["电工工时", "497.5 × 0.52", "258.7 h"],
    ["公摊工时", "258.7 + 258.7", "517.4 h"],
    ["包装后面积", "199.7 × 1.15", "229.7 m²"],
    ["箱数（中）", "⌈ 229.7 ÷ 18.38 ⌉ = ⌈ 12.50 ⌉", "13 箱（含框架箱 1 箱）"],
    ["运费（中）", "12 × 5,497 + 1 × 9,494", "US$75,458"],
    ["箱数、运费（低）", "⌈ 229.7 ÷ 21.20 ⌉ = ⌈ 10.83 ⌉ = 11 箱，× 5,896.70", "US$64,864"],
    ["箱数、运费（高）", "⌈ 229.7 ÷ 15.55 ⌉ = ⌈ 14.77 ⌉ = 15 箱，× 5,896.70", "US$88,451"],
  ], [2400, 4338, 2900], { numeric: [2] }),
  note("每个 40HQ 的可用底面积：中 18.38 m² = 28.27 m² × 0.65；低 21.20 m² = 28.27 m² × 0.75；高 15.55 m² = 28.27 m² × 0.55。"),

  h1("八、四张图纸汇总"),
  caption("表 1  面积、工时与箱数"),
  table(["图纸", "精细面积 m²", "基准面积 m²", "公摊工时 h", "40HQ 低 / 中 / 高", "框架箱（中）"], [
    ["101", "199.7", "497.5", "517", "11 / 13 / 15", "1"],
    ["102", "1,457.4", "4,883.2", "5,079", "80 / 92 / 108", "9"],
    ["103", "572.8", "1,424.4", "1,481", "32 / 36 / 43", "4"],
    ["104", "1,329.8", "3,272.8", "3,404", "73 / 84 / 99", "8"],
    ["合计", "3,559.7", "10,078.0", "10,481", "196 / 225 / 265", "22"],
  ], [1000, 1700, 1700, 1600, 2138, 1500], { numeric: [1, 2, 3, 4, 5], totalRow: true }),
  caption("表 2  海运费（US$）"),
  table(["图纸", "低（乐观）", "中", "高（保守）"], [
    ["101", "64,864", "75,458", "88,451"],
    ["102", "471,736", "541,697", "636,844"],
    ["103", "188,694", "213,880", "253,558"],
    ["104", "430,459", "493,724", "583,773"],
    ["合计", "1,155,753", "1,324,759", "1,562,626"],
  ], [1600, 2679, 2680, 2679], { numeric: [1, 2, 3], totalRow: true }),
  caption("表 3  基准面积明细（各线体总长 × 总宽）"),
  table(["图纸", "线体数", "各线体 长 × 宽（m）", "基准面积 m²"], [
    ["101", "1", "72.1 × 6.9", "497.5"],
    ["102", "2", "108.1 × 22.4；111.9 × 22.0", "4,883.2"],
    ["103", "1", "107.1 × 13.3", "1,424.4"],
    ["104", "2", "79.0 × 19.5；80.2 × 21.6", "3,272.8"],
  ], [1400, 1400, 4438, 2400], { numeric: [1, 3] }),
  note("合计按未取整的数值相加后再取整，因此个别合计与表中各项相加在末位上可能略有差别。"),

  h1("九、参数来源与可信度"),
  para("只有面积是从图纸测量得到的；其余全部是业务输入，可在网页“估算参数”中修改。"),
  table(["参数", "取值", "来源", "性质"], [
    ["设备投影面积、线体总长 × 宽", "见第八节", "DXF 图纸几何计算", "测量值"],
    ["钳工 / 电工公摊工时系数", "0.52 / 0.52 h/m²", "《钳工/电工工时推算》表", "业务输入"],
    ["公摊工时面积口径", "基准（总长 × 宽）", "用户确认，2026-11-02", "业务决定"],
    ["包装系数", "1.15", "经验假设", "待用历史装柜数据校准"],
    ["地板利用率", "0.55（保守）– 0.75（乐观）", "经验假设", "待校准"],
    ["超限设备比例", "10%", "经验假设", "待校准"],
    ["海运价（上海 → 鹿特丹）", "US$3,997 / 40HQ", "Drewry WCI 现货价，2026-09-10", "已约 8 周，报价前需更新"],
    ["框架箱 / 开顶箱海运价（上海 → 鹿特丹）", "US$7,994 / 柜", "占位值 = 40HQ 运价 × 2", "需用货代报价替换"],
    ["起运港 + 目的港杂费", "US$1,500 / 箱", "占位值", "需用货代报价替换"],
    ["保险", "0", "未计入", "需确认"],
    ["40HQ 内尺寸", "12.03 × 2.35 × 2.69 m", "标准箱内尺寸", "固定值"],
  ], [2600, 2238, 2600, 2200]),

  h1("十、未考虑的因素与已知局限"),
  bullet([{ t: "重量：", b: true }, "未校核 40HQ 载重上限（26.5 t）。重型设备可能由重量而不是面积决定箱数。"]),
  bullet([{ t: "高度与堆叠：", b: true }, "只通过“超限设备比例”粗略考虑，未逐台判断设备尺寸。"]),
  bullet([{ t: "拼箱：", b: true }, "每张图纸单独向上取整。四条线合并装运时，中间情形为 223 箱，而不是 225 箱。"]),
  bullet([{ t: "运费口径：", b: true }, "低、高情形按加权箱价计算，中间情形按整数框架箱数计算，三者口径略有差别。"]),
  bullet([{ t: "工位：", b: true }, "DXF 暂不拆分工位，精细面积为整张图的设备投影面积。"]),
  bullet([{ t: "102 图的两条长条：", b: true }, "约 22 m × 0.92 m（各约 20 m²），目前计为设备，可能是建筑构件。它们使线体外包变宽，并使精细面积多约 40 m²，需工程确认。"]),
  bullet([{ t: "单列不计的面积：", b: true }, "移动设备和待定物体不计入精细面积，例如 102 图的移动设备 59.1 m²、待定物体 113.3 m²。"]),
  bullet([{ t: "取整：", b: true }, "基准面积按长、宽各取 0.1 m 计算（与工时表一致），例如 101 为 497.5 m²，精确值为 496.3 m²。"]),

  h1("十一、如何复核与调整"),
  bullet("网页“估算参数”中的所有业务参数都可以修改，提交后重新计算；默认值来自 config.yaml。"),
  bullet("导出的 Excel 工作簿『汇总』页使用相同公式（Excel 公式，改参数自动重算），每个数字都能追溯到单元格。"),
  bullet("每张图纸附有叠加图：绿色 = 计入的设备轮廓，青色框 L1、L2… = 各线体外包（基准面积），红框 = 排除，紫框 = 移动设备，橙框 = 待定。可直接核对是否覆盖了实际设备、是否多算或漏算。"),
];

const doc = new Document({
  creator: "layout-estimator",
  title: "产线投影面积、公摊工时与运费估算——计算逻辑说明",
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 30, bold: true, color: TEAL },
        paragraph: { spacing: { before: 360, after: 140 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 24, bold: true, color: ACCENT },
        paragraph: { spacing: { before: 220, after: 100 }, keepNext: true, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
                                          style: { paragraph: { indent: { left: 420, hanging: 260 } } } }] },
      { reference: "steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
                                        style: { paragraph: { indent: { left: 420, hanging: 320 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 },
                          margin: { top: 1247, bottom: 1247, left: 1134, right: 1134, header: 600, footer: 600 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
      children: [new TextRun({ text: "产线布局投影面积估算 · 计算逻辑说明", size: 16, color: GREY })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: ["第 ", PageNumber.CURRENT, " 页 / 共 ", PageNumber.TOTAL_PAGES, " 页"], size: 16, color: GREY })] })] }) },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, buf.length, "bytes"); });
