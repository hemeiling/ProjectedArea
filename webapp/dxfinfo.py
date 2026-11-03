"""
上传文件的类型识别（按内容，不按扩展名）与 DXF 文件头信息。

DXF 文件头（HEADER 段）在文件最前面，只读这一段就能知道单位（$INSUNITS）和图形范围（$EXTMIN/$EXTMAX），
不必把几百 MB 的整个图读进内存。图形范围只作为“选单位”时的参考提示，面积计算不用它。
"""
import re

from ezdxf import units as ezunits
from ezdxf.filemanagement import dxf_file_info

PDF, DXF, DWG = "pdf", "dxf", "dwg"
# ASCII DXF：可有 999 注释行，第一组必须是 0 / SECTION；二进制 DXF 有固定标识
_ASCII_DXF = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*(?:999\s*\r?\n[^\n]*\n\s*)*0\s*\r?\n\s*SECTION\s*\r?\n")
_BINARY_DXF = b"AutoCAD Binary DXF\r\n\x1a\x00"
_DWG = re.compile(rb"\AAC10\d\d")
HEADER_MAX_BYTES = 8 * 1024 * 1024          # HEADER 段一般只有几十 KB；超过此长度仍未结束就不再找
# 上传时可选的单位：每个图纸单位等于多少 mm（只在文件未声明单位时使用）
UNIT_CHOICES_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


def sniff(head):
    """文件开头的字节 -> pdf / dxf / dwg / None"""
    if b"%PDF-" in head[:1024]:
        return PDF
    if head.startswith(_BINARY_DXF) or _ASCII_DXF.match(head[:4096]):
        return DXF
    if _DWG.match(head[:6]):
        return DWG
    return None


def _ascii_header(path):
    """ASCII DXF 的 HEADER 段：{变量名: 值}，坐标值为 (x, y)"""
    out, name, xy = {}, None, {}
    with open(path, "rb") as f:
        read = 0
        while read < HEADER_MAX_BYTES:
            code_line, value_line = f.readline(), f.readline()
            if not value_line:
                break
            read += len(code_line) + len(value_line)
            code, value = code_line.strip(), value_line.strip().decode("ascii", "replace")
            if code == b"0" and value == "ENDSEC":
                break
            if code == b"9":
                if name and xy:
                    out[name] = (xy.get(10), xy.get(20))
                name, xy = value, {}
            elif name and code in (b"10", b"20"):
                xy[int(code)] = float(value)
            elif name and code in (b"1", b"70"):
                out[name] = value
    if name and xy:
        out[name] = (xy.get(10), xy.get(20))
    return out


def header_info(path):
    """-> dict(version, insunits, units_mm（未声明为 None）, extents（图纸单位的 宽, 高；读不到为 None）)。
    文件不是可读的 DXF 时抛出异常（由调用方转成提示）。"""
    info = dxf_file_info(path)
    insunits = int(info.insert_units or 0)
    extents = None
    try:
        h = _ascii_header(path)
        lo, hi = h.get("$EXTMIN"), h.get("$EXTMAX")
        if lo and hi and None not in lo + hi:
            w, ht = hi[0] - lo[0], hi[1] - lo[1]
            if 0 < w < 1e12 and 0 < ht < 1e12:          # 未设置的范围是 ±1e20
                extents = (w, ht)
    except (OSError, ValueError):
        extents = None                                   # 二进制 DXF 或文件头异常：只是少一个提示
    units_mm = ezunits.conversion_factor(insunits, ezunits.MM) if insunits else None
    return dict(version=info.version, release=info.release, insunits=insunits,
                units_name=ezunits.decode(insunits) if insunits else None, units_mm=units_mm, extents=extents)
