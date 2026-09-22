"""ProcessOn chart definition -> Visio .vsdx exporter.

Turns the elements JSON returned by ProcessOn's
`/api/personal/diagraming/get/chart/def` into a standalone .vsdx file
(Open Packaging Convention: a ZIP of XML parts). Pure Python, no Visio
required. Covers the shapes our flowchart tool actually produces:
rectangle / decision (diamond) / terminator, linker edges (straight or
elbow), and group containers.

The VSDX package layout follows the minimal known-good skeleton (same as
the open-source bpmn-to-visio approach):
  [Content_Types].xml, _rels/.rels, visio/document.xml(+rels),
  visio/pages/pages.xml(+rels), visio/pages/page1.xml, visio/windows.xml,
  docProps/app.xml.
"""
from __future__ import annotations

import zipfile
from io import BytesIO
from typing import Any, Dict, List, Tuple

PPI = 96


def _is_container_frame(el: dict) -> bool:
    """Group frames are the light-blue fill rectangles (224,234,244)."""
    col = (el.get("fillStyle") or {}).get("color")
    if isinstance(col, str):
        parts = [v.strip() for v in col.split(",")]
        try:
            r, g, b = (int(float(v)) for v in parts[:3])
        except ValueError:
            return False
        return abs(r - 224) < 12 and abs(g - 234) < 12 and abs(b - 244) < 12
    return bool((el.get("attribute") or {}).get("container"))


PPI = 96.0  # ProcessOn pixels -> inches (96 px == 1 in)


def _r(v: float) -> float:
    return round(float(v), 4)


def _escape_xml(t: str) -> str:
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _rgb_to_visio(color: str, default: str) -> str:
    """ProcessOn 'r,g,b' or '#RRGGBB' -> Visio '#RRGGBB'."""
    if not color:
        return default
    color = str(color).strip()
    if color.startswith("#"):
        return color
    parts = color.split(",")
    if len(parts) == 3:
        try:
            r, g, b = (int(float(p)) for p in parts)
            return f"#{r:02X}{g:02X}{b:02X}"
        except ValueError:
            return default
    return default


def _text_of(el: Dict[str, Any]) -> str:
    for tb in el.get("textBlock") or []:
        if tb.get("text"):
            return tb["text"]
    return el.get("title") or ""


def _rect_geom(w: float, h: float) -> str:
    return (
        '<Section N="Geometry" IX="0"><Cell N="NoFill" V="0"/>'
        '<Cell N="NoLine" V="0"/>'
        f'<Row T="MoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
        f'<Row T="LineTo" IX="2"><Cell N="X" V="{_r(w)}"/><Cell N="Y" V="0"/></Row>'
        f'<Row T="LineTo" IX="3"><Cell N="X" V="{_r(w)}"/><Cell N="Y" V="{_r(h)}"/></Row>'
        f'<Row T="LineTo" IX="4"><Cell N="X" V="0"/><Cell N="Y" V="{_r(h)}"/></Row>'
        '</Section>')


def _diamond_geom(w: float, h: float) -> str:
    hw, hh = w / 2, h / 2
    return (
        '<Section N="Geometry" IX="0"><Cell N="NoFill" V="0"/>'
        '<Cell N="NoLine" V="0"/>'
        f'<Row T="MoveTo" IX="1"><Cell N="X" V="{_r(hw)}"/><Cell N="Y" V="0"/></Row>'
        f'<Row T="LineTo" IX="2"><Cell N="X" V="{_r(w)}"/><Cell N="Y" V="{_r(hh)}"/></Row>'
        f'<Row T="LineTo" IX="3"><Cell N="X" V="{_r(hw)}"/><Cell N="Y" V="{_r(h)}"/></Row>'
        f'<Row T="LineTo" IX="4"><Cell N="X" V="0"/><Cell N="Y" V="{_r(hh)}"/></Row>'
        '</Section>')


def _line_geom(pts: List[Tuple[float, float]], bb_min_x: float,
               bb_min_y: float) -> str:
    """Polyline in local coords (shape bbox origin = bottom-left)."""
    rows = []
    for i, (x, y) in enumerate(pts):
        lx, ly = _r(x - bb_min_x), _r(y - bb_min_y)
        tag = "MoveTo" if i == 0 else "LineTo"
        rows.append(f'<Row T="{tag}" IX="{i + 1}">'
                    f'<Cell N="X" V="{lx}"/><Cell N="Y" V="{ly}"/></Row>')
    return ('<Section N="Geometry" IX="0"><Cell N="NoFill" V="1"/>'
            '<Cell N="NoLine" V="0"/>' + "".join(rows) + '</Section>')


def _shape_xml(sid: int, pin_x: float, pin_y: float, w: float, h: float,
              geom: str, text: str, fill: str, line: str,
              line_weight: str = "0.014", no_fill: bool = False,
              text_color: str = "#FFFFFF", text_top: bool = False) -> str:
    text_xml = f"<Text>{_escape_xml(text)}</Text>" if text else ""
    if not no_fill:
        fill_cell = (f'<Cell N="FillForegnd" V="{fill}"/>'
                     '<Cell N="FillPattern" V="1"/>')
    else:
        fill_cell = ('<Cell N="FillForegnd" V="#FFFFFF"/>'
                     '<Cell N="FillPattern" V="0"/>')
    tw, th = _r(w), _r(h)
    if text_top and text:
        txt_py = _r(h - 0.12); txt_h = _r(min(0.3, h)); txt_lp = _r(txt_h / 2)
    else:
        txt_py = _r(h / 2); txt_h = th; txt_lp = _r(h / 2)
    return (
        f'<Shape ID="{sid}" NameU="Shape.{sid}" Type="Shape">'
        f'<Cell N="PinX" V="{_r(pin_x)}"/><Cell N="PinY" V="{_r(pin_y)}"/>'
        f'<Cell N="Width" V="{_r(w)}"/><Cell N="Height" V="{_r(h)}"/>'
        f'<Cell N="LocPinX" V="{_r(w/2)}" F="Width*0.5"/>'
        f'<Cell N="LocPinY" V="{_r(h/2)}" F="Height*0.5"/>'
        '<Cell N="Angle" V="0"/><Cell N="FlipX" V="0"/><Cell N="FlipY" V="0"/>'
        '<Cell N="LineColor" V="{}"/>'.format(line) +
        f'<Cell N="LineWeight" V="{line_weight}"/>'
        f'{fill_cell}'
        f'<Cell N="TxtPinX" V="{_r(w/2)}"/>'
        f'<Cell N="TxtPinY" V="{txt_py}"/>'
        f'<Cell N="TxtWidth" V="{tw}"/><Cell N="TxtHeight" V="{txt_h}"/>'
        f'<Cell N="TxtLocPinX" V="{_r(w/2)}"/>'
        f'<Cell N="TxtLocPinY" V="{txt_lp}"/>'
        '<Section N="Character" IX="0"><Row IX="0">'
        '<Cell N="Font" V="0"/><Cell N="Size" V="0.1111"/>'
        f'<Cell N="Color" V="{text_color}"/></Row></Section>'
        '<Section N="Paragraph" IX="0"><Row IX="0">'
        '<Cell N="HorzAlign" V="1"/></Row></Section>'
        f'{geom}{text_xml}</Shape>')


def export_def_to_vsdx(elements: Dict[str, Any], out_path: str,
                       page_name: str = "ProcessOn Diagram") -> str:
    """Render ProcessOn elements to a .vsdx file. Returns out_path."""
    nodes: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    for el in elements.values():
        if not isinstance(el, dict):
            continue
        name = el.get("name")
        if name == "linker":
            links.append(el)
        elif el.get("props", {}).get("w"):
            nodes.append(el)

    # pixel bounds
    all_x, all_y = [], []
    for n in nodes:
        p = n["props"]
        all_x += [p["x"], p["x"] + p["w"]]
        all_y += [p["y"], p["y"] + p["h"]]
    for l in links:
        fr, to = l.get("from", {}), l.get("to", {})
        for pt in ([fr, to] + (l.get("points") or [])):
            if pt.get("x") is not None:
                all_x.append(pt["x"]); all_y.append(pt["y"])
    if not all_x:
        all_x, all_y = [0, 1056], [0, 816]
    margin = 40
    min_x, min_y = min(all_x) - margin, min(all_y) - margin
    max_x, max_y = max(all_x) + margin, max(all_y) + margin
    page_w = max((max_x - min_x) / PPI, 11.0)
    page_h = max((max_y - min_y) / PPI, 8.5)

    def to_vx(px: float) -> float:
        return (px - min_x) / PPI

    def to_vy(py: float) -> float:
        return page_h - (py - min_y) / PPI

    # z-order: containers (frames) FIRST (bottom), then normal nodes on top,
    # then links. If a container renders ABOVE the nodes it encloses, drawio /
    # ProcessOn treat the covered nodes as children and drop them.
    containers = [n for n in nodes if _is_container_frame(n)]
    normals = [n for n in nodes if not _is_container_frame(n)]
    ordered_nodes = containers + normals

    shape_xmls: List[str] = []
    sid = 1
    for n in ordered_nodes:
        p = n["props"]
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        pin_x = to_vx(x + w / 2)
        pin_y = to_vy(y + h / 2)
        wi, hi = w / PPI, h / PPI
        name = n.get("name", "rectangle")
        if name == "decision":
            geom = _diamond_geom(wi, hi)
        else:
            geom = _rect_geom(wi, hi)
        fill = _rgb_to_visio((n.get("fillStyle") or {}).get("color"), "#005B99")
        line = _rgb_to_visio((n.get("lineStyle") or {}).get("lineColor"), "#004370")
        # containers: light-blue SOLID fill (224,234,244), dark text on top.
        # Do NOT set no_fill here — the frame is a filled rectangle, not a
        # transparent dashed box. Only normal node labels are light-on-dark.
        is_frame = _is_container_frame(n)
        if is_frame:
            no_fill = False
            tcolor = "#333333"
            text_top = True
        else:
            no_fill = False
            text_top = False
            tcolor = _rgb_to_visio((n.get("fontStyle") or {}).get("color"),
                                   "#E9F1F9")
        shape_xmls.append(_shape_xml(sid, pin_x, pin_y, wi, hi, geom,
                                     _text_of(n), fill, line, no_fill=no_fill,
                                     text_color=tcolor, text_top=text_top))
        sid += 1

    for l in links:
        fr, to = l.get("from", {}), l.get("to", {})
        pts = []
        for pt in ([fr] + (l.get("points") or []) + [to]):
            if pt.get("x") is not None and pt.get("y") is not None:
                pts.append((to_vx(pt["x"]), to_vy(pt["y"])))
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        bb_min_x, bb_max_x = min(xs), max(xs)
        bb_min_y, bb_max_y = min(ys), max(ys)
        w = max(bb_max_x - bb_min_x, 0.02)
        h = max(bb_max_y - bb_min_y, 0.02)
        pin_x = (bb_min_x + bb_max_x) / 2
        pin_y = (bb_min_y + bb_max_y) / 2
        line_c = _rgb_to_visio((l.get("lineStyle") or {}).get("lineColor"),
                               "#444444")
        geom = _line_geom(pts, bb_min_x, bb_min_y)
        shape_xmls.append(_shape_xml(sid, pin_x, pin_y, w, h, geom,
                                     _text_of(l), "#FFFFFF", line_c,
                                     line_weight="0.012"))
        sid += 1

    shapes_xml = "\n".join(shape_xmls)
    page_name_esc = _escape_xml(page_name)

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
        '<Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>'
        '<Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/>'
        '<Override PartName="/visio/windows.xml" ContentType="application/vnd.ms-visio.windows+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<VisioDocument xmlns="http://schemas.microsoft.com/office/visio/2012/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<DocumentProperties><Creator>mcp_processon</Creator></DocumentProperties>'
        '<DocumentSettings/><Colors/><FaceNames>'
        '<FaceName ID="0" Name="Calibri" UnicodeRanges="-1 -1 0 0" CharSets="536871423 0" Panos="2 15 5 2 2 2 4 3 2 4"/>'
        '</FaceNames><StyleSheets><StyleSheet ID="0" NameU="Normal" Name="Normal">'
        '<Cell N="LineWeight" V="0.01"/><Cell N="LineColor" V="#333333"/>'
        '<Cell N="FillForegnd" V="#FFFFFF"/><Cell N="CharFont" V="0"/>'
        '<Cell N="TxtHeight" V="0.1111"/></StyleSheet></StyleSheets>'
        '</VisioDocument>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/pages" Target="pages/pages.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.microsoft.com/visio/2010/relationships/windows" Target="windows.xml"/>'
        '</Relationships>')
    pages = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Pages xmlns="http://schemas.microsoft.com/office/visio/2012/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<Page ID="0" NameU="{page_name_esc}" Name="{page_name_esc}"><PageSheet>'
        f'<Cell N="PageWidth" V="{_r(page_w)}"/><Cell N="PageHeight" V="{_r(page_h)}"/>'
        '<Cell N="DrawingScale" V="1"/><Cell N="PageScale" V="1"/></PageSheet>'
        '<Rel r:id="rId1"/></Page></Pages>')
    pages_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/page" Target="page1.xml"/>'
        '</Relationships>')
    page1 = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<Shapes>{shapes_xml}</Shapes></PageContents>')
    windows = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Windows xmlns="http://schemas.microsoft.com/office/visio/2012/main">'
        '<Window ID="0" WindowType="Drawing" WindowState="1073741824" WindowLeft="0" WindowTop="0" WindowWidth="1024" WindowHeight="768">'
        '<ShowRulers>1</ShowRulers><ShowGrid>1</ShowGrid><ShowPageBreaks>0</ShowPageBreaks>'
        '<ShowGuides>1</ShowGuides><ShowConnectionPoints>1</ShowConnectionPoints>'
        '<GlueSettings>9</GlueSettings><SnapSettings>65847</SnapSettings>'
        '<SnapExtensions>34</SnapExtensions><DynamicGridEnabled>1</DynamicGridEnabled>'
        '<TabSplitterPos>0.5</TabSplitterPos></Window></Windows>')
    app_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>mcp_processon</Application></Properties>')

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("visio/document.xml", document)
        zf.writestr("visio/_rels/document.xml.rels", doc_rels)
        zf.writestr("visio/pages/pages.xml", pages)
        zf.writestr("visio/pages/_rels/pages.xml.rels", pages_rels)
        zf.writestr("visio/pages/page1.xml", page1)
        zf.writestr("visio/windows.xml", windows)
        zf.writestr("docProps/app.xml", app_xml)
    return out_path
