#!/usr/bin/env python3
"""
results/results.json + config.yaml -> 估算工作簿 (Excel)。图纸数量不限。

用法:
    python build_workbook.py config.yaml results/results.json 输出.xlsx
"""
import json, sys, os
import yaml
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter as CL
from openpyxl.drawing.image import Image as XImg
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image

cfg_path, res_path, out = sys.argv[1:4]
cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
res = json.load(open(res_path, encoding="utf-8"))
res_dir = os.path.dirname(os.path.abspath(res_path))
D = res["drawings"]; N = len(D)

FN = "Arial"
F = lambda **k: Font(name=FN, size=k.pop("size", 10), **k)
BLUE, GREEN, BOLD = F(color="0000FF"), F(color="008000"), F(bold=True)
HDR_F, HDR = F(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F2D44")
YEL, KEY, GREY = PatternFill("solid", fgColor="FFFF00"), PatternFill("solid", fgColor="FBE5D6"), PatternFill("solid", fgColor="F2F2F2")
th = Side(style="thin", color="BFBFBF"); BOX = Border(top=th, bottom=th, left=th, right=th)
NOTE = F(color="595959", size=9)


def header(ws, row, labels, col0=1):
    for i, l in enumerate(labels):
        c = ws.cell(row, col0 + i, l); c.font = HDR_F; c.fill = HDR
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")


def put(ws, ref, v, fmt=None, font=None, fill=None):
    c = ws[ref]; c.value = v; c.border = BOX
    if fmt: c.number_format = fmt
    c.font = font or F()
    if fill: c.fill = fill
    return c


wb = Workbook()

# ===================================================== 参数
pa = wb.active; pa.title = "参数"
pa.column_dimensions["A"].width = 40; pa.column_dimensions["B"].width = 18; pa.column_dimensions["C"].width = 80
pa["A1"] = "估算参数（蓝字为可修改输入，黄底为需用历史数据校准的关键假设）"; pa["A1"].font = F(bold=True, size=12)
P = {}
pk, fr, lb = cfg["packing"], cfg["freight"], cfg["labor"]
rows = [
    ("工时", None, None, None),
    ("ratio_h", "公摊工时系数（工时 / m²）", lb["shared_hours_per_m2"], "0.00", lb.get("source", ""), True),
    ("装箱", None, None, None),
    ("pack", "包装系数（包装后面积 / 投影面积）", pk["packaging_factor"], "0.00", "木箱每边约 +100 mm、加底托。建议用历史装柜数据反推", True),
    ("cL", f"{pk['container']['name']} 内长 (m)", pk["container"]["inner_length_m"], "0.00", "40HQ 标准内尺寸 12.03 × 2.35 × 2.69 m，限重约 26.5 t", False),
    ("cW", f"{pk['container']['name']} 内宽 (m)", pk["container"]["inner_width_m"], "0.00", "", False),
    ("ulo", "地板利用率 – 保守", pk["floor_utilisation"]["low"], "0%", "设备不可堆叠、外形不规则、需绑扎空间", True),
    ("uhi", "地板利用率 – 乐观", pk["floor_utilisation"]["high"], "0%", "", True),
    ("flo", "面积测量误差 – 下限系数", pk["footprint_uncertainty"]["low"], "0.00", "浅灰色设备填充不全 vs 少量线条噪声", False),
    ("fhi", "面积测量误差 – 上限系数", pk["footprint_uncertainty"]["high"], "0.00", "", False),
    ("os", "超限设备比例（框架箱/开顶箱）", pk["oversize_share"], "0%", "宽 >2.35 m 或高 >2.6 m 的设备；PDF 无高度信息，需补充设备总图", True),
    ("运费", None, None, None),
    ("route", "目的航线", fr["route"], "@", "下拉选择", True),
    ("rate", "该航线海运价 (US$/40ft)", None, "$#,##0", "由下方运价表查得", False),
    ("frm", "框架箱运价倍数", fr["flat_rack_multiplier"], "0.0x", "特种箱一般为普通箱 1.5–2.5 倍，以货代报价为准", False),
    ("loc", "起运港+目的港杂费 (US$/箱)", fr["local_charges_usd_per_box"], "$#,##0", "占位值：THC、文件、报关、短驳。用货代报价替换", True),
    ("ins", "保险（占运费比例）", fr["insurance_pct_of_freight"], "0%", "一般按货值投保，默认 0", False),
]
r = 3
for row in rows:
    if row[1] is None:
        pa.cell(r, 1, row[0]).font = F(bold=True, color="FFFFFF"); pa.cell(r, 1).fill = HDR
        pa.cell(r, 2).fill = HDR; pa.cell(r, 3).fill = HDR; r += 1; continue
    key, lab, val, fmt, note, keyflag = row
    pa.cell(r, 1, lab).font = F()
    ref = f"B{r}"
    if val is not None:
        put(pa, ref, val, fmt, BLUE, YEL if keyflag else None)
    pa.cell(r, 3, note).font = NOTE
    P[key] = f"参数!$B${r}"; r += 1
r += 1
pa.cell(r, 1, "海运现货运价参考").font = BOLD; r += 1
header(pa, r, ["航线", "US$ / 40ft", "来源"]); r += 1
rt0 = r
for name, v in fr["rates_usd_per_40ft"].items():
    pa.cell(r, 1, name).font = F(); put(pa, f"B{r}", v, "$#,##0", BLUE); r += 1
rt1 = r - 1
pa.cell(rt0, 3, fr["rates_source"] + " —— 运价每周变化，报价前请更新").font = NOTE
rate_row = int(P["rate"].split("$")[-1])
pa[f"B{rate_row}"] = f"=INDEX($B${rt0}:$B${rt1},MATCH({P['route'].replace('参数!','')},$A${rt0}:$A${rt1},0))"
pa[f"B{rate_row}"].number_format = "$#,##0"; pa[f"B{rate_row}"].border = BOX; pa[f"B{rate_row}"].font = F()
dv = DataValidation(type="list", formula1=f"=$A${rt0}:$A${rt1}"); pa.add_data_validation(dv)
dv.add(P["route"].replace("参数!", "").replace("$", ""))

# ===================================================== 工位明细
st = wb.create_sheet("工位明细", 0)
cols = ["图纸", "工位编号", "工位名称", "实际投影面积 (m²)", "工位外包 长 (m)", "工位外包 宽 (m)",
        "工位外包面积 (m²)", "投影/外包", "公摊工时 (h)", "OCR 原文", "编号为推断"]
widths = [10, 10, 34, 16, 13, 13, 15, 11, 13, 46, 11]
st["A1"] = "各工位实际占地（投影）面积 —— 由图纸自动识别"; st["A1"].font = F(bold=True, size=13)
st["A2"] = "实际投影面积 = 工位区段内设备轮廓所围面积（不含通道/空地）；工位外包面积 = 该工位设备的外包矩形，供对比。"; st["A2"].font = NOTE
header(st, 4, cols)
r = 5
for d in D:
    for s in d["stations"]:
        vals = [d["id"], s["op"], s["name"], s["footprint_m2"], s["bbox_length_m"], s["bbox_width_m"]]
        for i, v in enumerate(vals, 1):
            c = st.cell(r, i, v); c.border = BOX; c.font = BLUE if i in (3, 4, 5, 6) else F()
            if i >= 4: c.number_format = "#,##0.0"
        put(st, f"G{r}", f"=E{r}*F{r}", "#,##0.0")
        put(st, f"H{r}", f"=IF(G{r}=0,\"-\",D{r}/G{r})", "0%")
        put(st, f"I{r}", f"=D{r}*{P['ratio_h']}", "#,##0")
        put(st, f"J{r}", s["ocr_text"], None, NOTE)
        put(st, f"K{r}", "是" if s["op_inferred"] else "", None, F(color="C00000") if s["op_inferred"] else F())
        r += 1
st_last = r - 1
for i, w in enumerate(widths, 1): st.column_dimensions[CL(i)].width = w
st.freeze_panes = "D5"; st.auto_filter.ref = f"A4:K{st_last}"

# ===================================================== 线体基准
ln = wb.create_sheet("线体基准（总长×宽）", 1)
ln["A1"] = "基准算法：产线总长 × 总宽（保留原粗算口径，作为对比）"; ln["A1"].font = F(bold=True, size=13)
ln["A2"] = "每条线体取设备外包矩形；一张图有多条并行线体时分别计算后相加（不含线体之间的通道）。"; ln["A2"].font = NOTE
header(ln, 4, ["图纸", "线体", "总长 (m)", "总宽 (m)", "总长×宽 (m²)", "该线体实际投影面积 (m²)", "投影/外包"])
r = 5
for d in D:
    for l in d["envelope"]["lanes"]:
        put(ln, f"A{r}", d["id"]); put(ln, f"B{r}", f"线体 {l['lane']}")
        put(ln, f"C{r}", l["length_m"], "0.0", BLUE); put(ln, f"D{r}", l["width_m"], "0.0", BLUE)
        put(ln, f"E{r}", f"=C{r}*D{r}", "#,##0.0"); put(ln, f"F{r}", l["footprint_m2"], "#,##0.0", BLUE)
        put(ln, f"G{r}", f"=IF(E{r}=0,\"-\",F{r}/E{r})", "0%"); r += 1
ln_last = r - 1
for c, w in zip("ABCDEFG", [10, 10, 12, 12, 16, 22, 12]): ln.column_dimensions[c].width = w

# ===================================================== 汇总
sm = wb.create_sheet("汇总", 0)
sm.column_dimensions["A"].width = 44
for i in range(N + 1): sm.column_dimensions[CL(2 + i)].width = 20
sm.column_dimensions[CL(N + 3)].width = 60
sm["A1"] = "投影面积 · 公摊工时 · 装柜与运费估算"; sm["A1"].font = F(bold=True, size=14)
sm["A2"] = "两种面积口径并列：基准（产线总长×宽）与精细（各工位实际投影面积相加）。参数在『参数』页修改。"; sm["A2"].font = NOTE
header(sm, 4, ["项目"] + [d["name"] for d in D] + ["合计", "计算方法"])
sm.row_dimensions[4].height = 32
TOT = CL(N + 2); NC = CL(N + 3)
lines = [
    # (label, formula-template using {c} {id}, fmt, note, sum?, highlight)
    ("图纸编号", "{id_lit}", "@", "", False, False),
    ("比例尺 (mm / PDF点)", "{mmpt}", "0.00", "自动识别：图上最长尺寸线 = 标注总尺寸", False, False),
    ("识别到的工位数", "=COUNTIF('工位明细'!$A$5:$A${st_last},{c}5)", "0", "OCR 识别 OP 编号；见『工位明细』", True, False),
    ("① 基准面积：产线总长×宽 (m²)", "=SUMIF('线体基准（总长×宽）'!$A$5:$A${ln_last},{c}5,'线体基准（总长×宽）'!$E$5:$E${ln_last})", "#,##0", "原粗算口径（保留作对比）", True, True),
    # 未识别到工位（如未安装 OCR）时，取整图投影面积（与各工位之和是同一口径，只是不分工位）
    ("② 精细面积：各工位投影面积之和 (m²)", "=IF(COUNTIF('工位明细'!$A$5:$A${st_last},{c}5)=0,{fp},SUMIF('工位明细'!$A$5:$A${st_last},{c}5,'工位明细'!$D$5:$D${st_last}))", "#,##0", "新算法：只计设备轮廓内面积；未识别到工位时取整图投影面积", True, True),
    ("精细 / 基准", "=IF({c}8=0,\"-\",{c}9/{c}8)", "0%", "产线中空地、通道占比越大，此值越小", False, False),
    ("公摊工时 – 按基准面积 (h)", f"={{c}}8*{P['ratio_h']}", "#,##0", "面积 × 公摊工时系数", True, False),
    ("公摊工时 – 按精细面积 (h)", f"={{c}}9*{P['ratio_h']}", "#,##0", "", True, True),
    ("装柜：包装后面积 (m²)", f"={{c}}9*{P['pack']}", "#,##0", "精细面积 × 包装系数", True, False),
    ("40HQ 箱数 – 低", f"=ROUNDUP({{c}}9*{P['flo']}*{P['pack']}/({P['cL']}*{P['cW']}*{P['uhi']}),0)", "0", "面积下限 + 乐观利用率", True, False),
    ("40HQ 箱数 – 中", f"=ROUNDUP({{c}}13/({P['cL']}*{P['cW']}*AVERAGE({P['ulo']},{P['uhi']})),0)", "0", "", True, True),
    ("40HQ 箱数 – 高", f"=ROUNDUP({{c}}9*{P['fhi']}*{P['pack']}/({P['cL']}*{P['cW']}*{P['ulo']}),0)", "0", "面积上限 + 保守利用率", True, False),
    ("   其中框架箱/开顶箱（中）", f"=ROUND({{c}}15*{P['os']},0)", "0", "", True, False),
    ("单箱费用 – 40HQ (US$)", f"={P['rate']}*(1+{P['ins']})+{P['loc']}", "$#,##0", "海运价 + 保险 + 杂费", False, False),
    ("单箱费用 – 框架箱 (US$)", f"={P['rate']}*{P['frm']}*(1+{P['ins']})+{P['loc']}", "$#,##0", "", False, False),
    ("运费 – 低 (US$)", "={c}14*((1-" + P["os"] + ")*{c}18+" + P["os"] + "*{c}19)", "$#,##0", "", True, False),
    ("运费 – 中 (US$)", "=({c}15-{c}17)*{c}18+{c}17*{c}19", "$#,##0", "", True, True),
    ("运费 – 高 (US$)", "={c}16*((1-" + P["os"] + ")*{c}18+" + P["os"] + "*{c}19)", "$#,##0", "", True, False),
]
for i, (lab, f, fmt, note, summ, hi) in enumerate(lines):
    rr = 5 + i
    sm.cell(rr, 1, lab).font = BOLD if hi else F()
    for j, d in enumerate(D):
        c = CL(2 + j)
        v = f.format(c=c, id_lit=d["id"], mmpt=d["mm_per_pt"], st_last=st_last, ln_last=ln_last, fp=d["footprint_m2"])
        if v.replace(".", "").isdigit():
            v = float(v) if "." in v else v
        cell = put(sm, f"{c}{rr}", v, fmt, BOLD if hi else F(), KEY if hi else None)
    if summ:
        put(sm, f"{TOT}{rr}", f"=SUM(B{rr}:{CL(N + 1)}{rr})", fmt, BOLD if hi else F(), KEY if hi else None)
    elif rr == 10:
        put(sm, f"{TOT}{rr}", f"=IF({TOT}8=0,\"-\",{TOT}9/{TOT}8)", fmt)
    sm.cell(rr, N + 3, note).font = NOTE
sm["A25"] = "说明：蓝字 = 输入/自动测量值；黄底 = 需校准的关键假设；橙底 = 关键结果。面积为规划级估算（约 ±10–15%），箱数与运费为 ±25%。"; sm["A25"].font = NOTE
sm["A26"] = "未包含：设备重量校核（PDF 无重量）、目的地内陆运输、关税、滞箱费。"; sm["A26"].font = NOTE
sm.freeze_panes = "B5"

# ===================================================== 方法说明 + 图
me = wb.create_sheet("方法说明")
me.column_dimensions["A"].width = 120
steps = [
    ("算法步骤（全部参数见 config.yaml，代码不针对具体图纸写死）", True),
    ("1. 读取矢量：用 PyMuPDF 读出 PDF 中每条线、矩形、曲线及其颜色与类型。文字是“填充+描边”路径，设备是纯描边，据此分离。", False),
    ("2. 比例尺：图上最长的线状路径 = 标注总尺寸线；mm/点 = 标注值 ÷ 线长。可在配置中直接指定覆盖。", False),
    ("3. 非设备区：长度 ≥60% 总尺寸的线围成厂房范围；单根 ≥1.5 m 的 45° 斜线成片区域 = 墙体/填充带，区内内容排除。", False),
    ("4. 非设备线条：黄色安全围栏、近白色水印字、长度 >4 m 的灰色通道线、长度 >14 m 的墙/轴线排除；AGV 转弯圆用 OpenCV 霍夫圆检测后擦除。", False),
    ("5. 面积：剩余设备线条栅格化 → 闭合 <150 mm 缝隙 → 填充 <20 m² 的封闭区（设备内部）→ 去除宽度 <250 mm 的细线 → 统计像素面积 = 实际投影面积。", False),
    ("6. 工位：黑色文字聚类成标签块，逐块 OCR（Tesseract）读出 OP 编号与名称；编号按同列连续性纠错/补全；名称可在配置中人工修正。", False),
    ("7. 分摊：每块设备面积归属横向最近的标签列；同列相邻标签中点为工位分界 → 各工位面积；所有工位相加 = 精细面积。", False),
    ("8. 基准：每条线体（被贯通填充带隔开）取设备外包矩形 总长×总宽，相加 = 原粗算口径，保留对比。", False),
    ("9. 装柜与运费：精细面积 × 包装系数 ÷（40HQ 地板面积 × 利用率）→ 箱数；箱数 × 单箱费用 → 运费。", False),
    ("已知局限：工位分界按标签中点划分，边界处相邻工位间会有少量面积互相归属；浅灰色设备可能填充不全；PDF 无设备高度与重量。", False),
    ("最佳长期方案：CAD 直接导出 DXF，每台设备为独立图块（Block）并带名称，可精确到单台设备，无需图像识别。", False),
]
for i, (t, b) in enumerate(steps, 1):
    me.cell(i, 1, t).font = F(bold=b, size=12 if b else 10)
    me.cell(i, 1).alignment = Alignment(wrap_text=True)
row = len(steps) + 2
for d in D:
    for key, cap in (("overlay", "红色 = 计入的投影面积"), ("station_overlay", "颜色 = 工位分摊区段")):
        if not d.get(key):
            continue
        me.cell(row, 1, f"{d['name']} —— {cap}").font = BOLD
        p = os.path.join(res_dir, d[key]); im = Image.open(p)
        img = XImg(p); h = 640; img.height = h; img.width = int(im.width * h / im.height)
        me.add_image(img, f"A{row + 1}"); row += 35

wb._sheets = [wb["汇总"], wb["工位明细"], wb["线体基准（总长×宽）"], wb["参数"], wb["方法说明"]]
wb.save(out)
print("saved", out)
