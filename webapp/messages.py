"""网页提示信息（中文 / English）。后端只返回 {code, zh, en}，前端按界面语言显示。

每条错误都说明：哪里出错、为什么、怎么处理。
"""

MESSAGES = {
    # ---- 上传校验
    "no_files": (
        "请至少选择一个 PDF 或 DXF 文件。",
        "Select at least one PDF or DXF file."),
    "too_many_files": (
        "一次最多 {max} 张图纸。",
        "At most {max} drawings per estimate."),
    "fields_mismatch": (
        "每张图纸都需要填写“标注总尺寸”。",
        "Every drawing needs its annotated overall dimension."),
    "file_too_large": (
        "「{file}」超过 {max_mb} MB 上限。",
        "\"{file}\" exceeds the {max_mb} MB limit."),
    "upload_too_large": (
        "本次上传的文件合计超过 {max_mb} MB，请分几批上传（图纸库里已有的文件不占上传量）。",
        "The files in this upload add up to more than {max_mb} MB; upload them in several batches "
        "(files already in the library do not count)."),
    "unsupported_file": (
        "「{file}」既不是有效的 PDF（开头没有 %PDF 标识），也不是 DXF（开头没有 0 / SECTION）。"
        "如果文件经过公司加密软件处理，请先解密；或在 AutoCAD 中重新“打印为 PDF”或“另存为 DXF”后再上传。",
        "\"{file}\" is neither a valid PDF (no %PDF header) nor a DXF (no 0 / SECTION at the start). If it was "
        "processed by file-encryption software, decrypt it first, or re-plot it to PDF or save it as DXF from AutoCAD."),
    "dwg_not_supported": (
        "「{file}」是 DWG 文件，本工具不能直接读取。请在 AutoCAD 中“另存为”DXF（任一版本）后上传。",
        "\"{file}\" is a DWG file, which this tool cannot read directly. Save it as DXF in AutoCAD "
        "(Save As → DXF, any version) and upload that."),
    "bad_dxf_unit": (
        "「{file}」的单位选择无效，请从列表中选择。",
        "The unit chosen for \"{file}\" is not valid; pick one from the list."),
    "bad_reference_length": (
        "「{file}」的标注总尺寸必须是大于 0 的毫米数（例如 75000）。",
        "The overall dimension for \"{file}\" must be a positive number of millimetres (e.g. 75000)."),
    "bad_mm_per_pt": (
        "「{file}」的比例（mm/点）必须是大于 0 的数字，或留空自动识别。",
        "The scale for \"{file}\" (mm per PDF point) must be a positive number, or left blank to detect it."),
    "bad_params": (
        "估算参数有误：{detail}",
        "Invalid estimate parameter: {detail}"),
    "bad_rows": (
        "提交的图纸列表格式有误，请刷新页面后重试。",
        "The submitted drawing list is malformed; reload the page and try again."),
    "library_missing": (
        "图纸库中找不到所选的历史图纸，请重新上传该文件。",
        "The selected drawing is no longer in the library; upload the file again."),
    "unauthorized": (
        "需要访问密码。",
        "Password required."),

    # ---- 任务执行
    "pdf_unreadable": (
        "「{file}」无法打开：{detail}。请确认文件完整、未加密。",
        "\"{file}\" could not be opened: {detail}. Check that the file is complete and not encrypted."),
    "no_vectors": (
        "「{file}」第 1 页没有矢量线条，可能是扫描件或图片 PDF，本工具不适用。请从 CAD 直接导出矢量 PDF。",
        "Page 1 of \"{file}\" has no vector linework; it is probably a scan or image PDF, which this tool "
        "cannot measure. Export a vector PDF directly from CAD."),
    "dxf_unreadable": (
        "「{file}」无法作为 DXF 读取：{detail}。请确认文件完整；必要时在 AutoCAD 中重新另存为 DXF。",
        "\"{file}\" could not be read as DXF: {detail}. Check that the file is complete; if needed, save it "
        "as DXF again from AutoCAD."),
    "dxf_killed": (
        "测量「{name}」（{size_mb} MB）时程序被系统结束，多半是内存不足：DXF 需要整个读入内存，"
        "峰值约为文件大小的 7–25 倍。请在内存更大的电脑上使用桌面版，或先在 AutoCAD 中删掉与产线无关的图形再另存。",
        "Measuring \"{name}\" ({size_mb} MB) was stopped by the system, most likely because it ran out of memory: "
        "the whole DXF has to be loaded, which peaks at about 7–25 times the file size. Use the desktop app on a "
        "computer with more memory, or delete geometry unrelated to the line in AutoCAD and save again."),
    "scale_not_found": (
        "「{name}」未找到尺寸线，无法自动确定比例尺。请在“比例（mm/点）”中直接填写后重新计算。",
        "No dimension line found in \"{name}\", so the scale could not be detected. Enter the scale "
        "(mm per PDF point) and run again."),
    "building_not_found": (
        "「{name}」未找到长度 ≥ 标注总尺寸 60% 的边界线。请核对“标注总尺寸”是否为图上最长尺寸线的标注值。",
        "No boundary line at least 60% of the annotated dimension was found in \"{name}\". Check that the "
        "overall dimension is the value written on the drawing's longest dimension line."),
    "measure_failed": (
        "测量程序在「{name}」出错，未得到结果。技术信息见下方。",
        "The measurement failed on \"{name}\"; no result was produced. Technical details below."),
    "workbook_failed": (
        "面积已算出，但生成 Excel 工作簿失败。技术信息见下方。",
        "Areas were measured, but building the Excel workbook failed. Technical details below."),
    "timeout": (
        "计算超过 {minutes} 分钟仍未完成，已停止。请减少一次上传的图纸数量后重试。",
        "The calculation did not finish within {minutes} minutes and was stopped. Try fewer drawings at once."),
    "interrupted": (
        "服务器重启，任务被中断，请重新提交。",
        "The server restarted and interrupted this estimate. Please submit it again."),
    "job_not_found": (
        "找不到该任务。",
        "Estimate not found."),
    "job_not_ready": (
        "任务尚未完成。",
        "The estimate has not finished yet."),

    "dimension_needed": (
        "缺少标注总尺寸：{names}。比例尺只能来自图上的标注数字（PDF 中的文字是图形，无法直接读取）。"
        "下图中红框标出的是程序用来定比例尺的那条线，请填写图上这个方向的总尺寸（mm），之后同一文件会自动沿用。",
        "Overall dimension missing: {names}. The scale can only come from the dimension printed on the drawing "
        "(text in these PDFs is drawn as shapes and cannot be read directly). The red box marks the line used for "
        "the scale; enter the drawing's overall dimension in that direction (mm). The same file will reuse it next time."),

    "dxf_units_needed": (
        "DXF 未声明单位（$INSUNITS = 0）：{names}。面积随单位的平方变化，不能默认为 mm。请为每张图选择"
        "“1 个图纸单位”等于多少；可在 AutoCAD 中量一台已知尺寸的设备核对。之后同一文件会自动沿用。",
        "The DXF does not declare its units ($INSUNITS = 0): {names}. Area scales with the square of the unit, "
        "so millimetres cannot be assumed. Choose what one drawing unit is for each drawing; you can check by "
        "measuring a machine of known size in AutoCAD. The same file will reuse it next time."),
    "inputs_needed": (
        "缺少测量输入：{names}。PDF 需要标注总尺寸，未声明单位的 DXF 需要选择单位；填写后继续。",
        "Measurement inputs missing: {names}. PDFs need the overall dimension and DXFs without declared units "
        "need a unit; fill them in to continue."),

    # ---- 结果提示（任务成功，但需人工注意）
    "dimension_remembered": (
        "「{name}」未填写标注总尺寸，沿用该文件上次的值 {value} mm（{when}）。如不正确请填写后重新计算。",
        "No overall dimension entered for \"{name}\"; reused this file's previous value {value} mm ({when}). "
        "If that is wrong, enter it and run again."),
    "reused_result": (
        "「{name}」与历史上传的文件完全相同（内容哈希一致），测量输入也相同，已直接复用 {when} 的测量结果。",
        "\"{name}\" is identical to an earlier upload (same content hash) with the same measurement inputs; "
        "the measurement from {when} was reused."),
    "ocr_unavailable": (
        "未找到 Tesseract OCR 程序，已跳过工位识别：只给出整图投影面积，不分工位；精细面积直接取整图投影面积（同一口径）。",
        "Tesseract OCR was not found, so station recognition was skipped: only the whole-drawing "
        "footprint is reported, not per station. The fine area uses that footprint (same definition)."),
    "multi_page": (
        "「{file}」共 {pages} 页，只计算第 1 页。",
        "\"{file}\" has {pages} pages; only page 1 was measured."),
    "no_stations": (
        "「{name}」未识别到 OP-xx 工位标签：精细面积取整图投影面积。",
        "No OP-xx station labels were recognised in \"{name}\"; the fine area uses the whole-drawing footprint."),
    "station_sum_mismatch": (
        "「{name}」各工位面积之和 {stations} m² 与整图投影面积 {total} m² 相差超过 1%，请查看工位叠加图。",
        "In \"{name}\" the station areas add up to {stations} m², which differs from the drawing footprint "
        "{total} m² by more than 1%. Check the station overlay."),
    "dxf_unit_ignored": (
        "「{name}」文件已声明单位为 {declared}，以文件为准，未使用网页上选择的单位。",
        "\"{name}\" declares its units as {declared}; the file's units were used, not the one chosen on the page."),
    "dxf_unit_reviewed": (
        "「{name}」DXF 未声明单位，按 config.yaml 中对此文件（哈希一致）已核对的记录取 {unit}。依据：{basis}",
        "\"{name}\" does not declare its units; used {unit} from the reviewed record for this exact file "
        "(same hash) in config.yaml. Basis: {basis}"),
    "dxf_unit_remembered": (
        "「{name}」DXF 未声明单位，沿用该文件上次的单位 {unit}（{when}）。如不正确请选择后重新计算。",
        "\"{name}\" does not declare its units; reused this file's previous unit {unit} ({when}). "
        "If that is wrong, choose the unit and run again."),
    "dxf_lanes": (
        "「{name}」DXF 基准（总长×宽）按线体分别计算：识别为 {n} 条线体。与产线长边平行、贯穿全图、宽 ≥ {gap} m 的空走道"
        "视为线体之间的分隔，不计入。公摊工时默认按基准计算，请在叠加图中核对青色线体框（L1、L2…）。",
        "For \"{name}\" the DXF baseline (length × width) is measured per line: {n} line(s) found. An empty aisle "
        "at least {gap} m wide, parallel to the long side and running the full length, separates lines and is not "
        "counted. Shared labour hours use the baseline by default; check the teal line boxes (L1, L2…) on the overlay."),
    "dxf_set_aside": (
        "「{name}」移动设备 {mobile} m²、待定 {unresolved} m² 单列，未计入投影面积（只计落在固定设备轮廓之外的部分）；"
        "见叠加图：紫框 = 移动，橙框 = 待定。",
        "In \"{name}\", mobile equipment ({mobile} m²) and unresolved items ({unresolved} m²) are listed "
        "separately and not counted in the footprint (only the part outside fixed equipment). See the overlay: "
        "purple = mobile, orange = unresolved."),
    "dxf_transform_check": (
        "「{name}」{count} 个图块插入的自检未通过（旋转、镜像或拉伸方向可能处理有误），面积需人工核对叠加图。",
        "{count} block inserts in \"{name}\" failed the transform self-check (rotation, mirroring or "
        "extrusion may be wrong); check the overlay before using the area."),
    "dxf_engine_note": (
        "「{name}」测量提示：{text}",
        "Measurement note for \"{name}\": {text}"),
    "dxf_engine_more": (
        "「{name}」另有 {count} 条测量提示，见任务目录中的 results.json。",
        "\"{name}\" has {count} more measurement notes; see results.json in the job folder."),
    "dxf_no_stations": (
        "「{name}」DXF 暂不分工位（工位识别目前只用于 PDF）：精细面积取整图投影面积。",
        "\"{name}\": stations are not split for DXF yet (station recognition only works on PDFs); "
        "the fine area uses the whole-drawing footprint."),
    "dxf_no_footprint": (
        "「{name}」没有识别出任何设备面积。请查看叠加图，并核对单位与图层（关闭、冻结、不打印的图层不计入）。",
        "No equipment area was found in \"{name}\". Check the overlay, the units and the layers (layers that "
        "are off, frozen or not plotted are not counted)."),
    "no_footprint": (
        "「{name}」没有识别出任何设备面积。请查看叠加图，并核对标注总尺寸。",
        "No equipment area was found in \"{name}\". Check the overlay and the overall dimension."),
    "inferred_ops": (
        "「{name}」有 {count} 个工位编号是按相邻编号推断的（工作簿中标“是”），请人工核对。",
        "{count} station numbers in \"{name}\" were inferred from their neighbours (marked in the workbook); "
        "please check them."),
}


def msg(code, **kw):
    zh, en = MESSAGES[code]
    return dict(code=code, zh=zh.format(**kw), en=en.format(**kw))
