"""Render bounded Android drawable trees as PNG; SVG is constructed here, never loaded from the APK."""
from __future__ import annotations

import base64
import math
import struct
from xml.etree import ElementTree as ET

import resvg_py

from . import apk

SIZE = 192
MAX_NODES = 256
MAX_SVG = 256 << 10


def _tree(raw):
    if struct.unpack_from("<H", raw)[0] != 3:
        raise ValueError("not compiled XML")
    strings, stack, root, nodes = [], [], None, 0
    for kind, header, start, end in apk._chunks(raw, 8, len(raw)):
        if kind == 1:
            count = struct.unpack_from("<I", raw, start + 8)[0]
            if count > 4096:
                raise ValueError("drawable string pool too large")
            strings, size = [], 0
            for i in range(count):
                value = apk._string(raw[start:end], i)
                size += len(value)
                if size > MAX_SVG:
                    raise ValueError("drawable strings too large")
                strings.append(value)
        elif kind == 0x102:
            nodes += 1
            if nodes > MAX_NODES or len(stack) >= 32:
                raise ValueError("drawable tree too large")
            ext = start + header
            _, name, offset, step, count = struct.unpack_from("<IIHHH", raw, ext)
            attrs = {}
            if count > 64 or step < 20 or ext + offset + step * count > end:
                raise ValueError("invalid drawable attributes")
            for i in range(count):
                _, key, text, _, _, dtype, value = struct.unpack_from("<IIIHBBI", raw, ext + offset + step * i)
                attrs[strings[key]] = (struct.unpack("<f", struct.pack("<I", value))[0] if dtype == 4
                                      else apk._value(strings, text, dtype, value))
            node = (strings[name], attrs, [])
            if stack:
                stack[-1][2].append(node)
            elif root is None:
                root = node
            else:
                raise ValueError("multiple drawable roots")
            stack.append(node)
        elif kind == 0x103:
            if not stack:
                raise ValueError("unbalanced drawable")
            stack.pop()
    if root is None or stack:
        raise ValueError("incomplete drawable")
    return root


def _number(value):
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e6:
        raise ValueError("invalid drawable coordinate")
    return number


def render_icon(archive, name):
    if archive.getinfo("resources.arsc").file_size > apk.MAX_ARSC:
        raise ValueError("resources too large")
    resources = archive.read("resources.arsc")
    budget = [apk.ICON_MAX]

    def resolve(value):
        seen = set()
        while isinstance(value, apk.Ref):
            if value.id in seen or len(seen) >= 8:
                raise ValueError("drawable resource cycle")
            seen.add(value.id)
            values = sorted(apk._arsc_values(resources, value.id), key=lambda v: (v[0] != "", -v[1]))
            if not values:
                raise ValueError("missing drawable resource")
            value = values[0][2]
        return value

    def color(value):
        value = resolve(value)
        if not isinstance(value, int):
            raise ValueError("unsupported drawable color")
        return f"#{value & 0xFFFFFF:06x}", (value >> 24 & 255) / 255

    def vector_children(parent, children):
        for tag, attrs, nested in children:
            if tag == "group":
                px, py = (_number(attrs.get(k, 0)) for k in ("pivotX", "pivotY"))
                tx, ty = (_number(attrs.get(k, 0)) for k in ("translateX", "translateY"))
                sx, sy = (_number(attrs.get(k, 1)) for k in ("scaleX", "scaleY"))
                rotation = _number(attrs.get("rotation", 0))
                group = ET.SubElement(parent, "g", transform=(f"translate({tx + px} {ty + py}) rotate({rotation}) "
                                                             f"scale({sx} {sy}) translate({-px} {-py})"))
                vector_children(group, nested)
            elif tag == "path" and not nested:
                if any(attrs.get(k, default) != default for k, default in
                       (("trimPathStart", 0), ("trimPathEnd", 1), ("trimPathOffset", 0))):
                    raise ValueError("trimmed vector paths unsupported")
                path = ET.SubElement(parent, "path", d=attrs["pathData"], fill="none", stroke="none")
                for paint in ("fill", "stroke"):
                    if paint + "Color" in attrs:
                        rgb, alpha = color(attrs[paint + "Color"])
                        path.set(paint, rgb)
                        path.set(paint + "-opacity", str(alpha * _number(attrs.get(paint + "Alpha", 1))))
                path.set("stroke-width", str(_number(attrs.get("strokeWidth", 0))))
                path.set("stroke-linecap", ("butt", "round", "square")[int(attrs.get("strokeLineCap", 0))])
                path.set("stroke-linejoin", ("miter", "round", "bevel")[int(attrs.get("strokeLineJoin", 0))])
                path.set("stroke-miterlimit", str(_number(attrs.get("strokeMiterLimit", 4))))
                path.set("fill-rule", "evenodd" if attrs.get("fillType") == 1 else "nonzero")
            else:
                raise ValueError("unsupported vector element")

    def drawable(value, seen=()):
        value = resolve(value)
        if isinstance(value, int):
            paint, alpha = color(value)
            return ET.Element("rect", width="108", height="108", fill=paint, opacity=str(alpha))
        if not isinstance(value, str) or value in seen or len(seen) >= 8:
            raise ValueError("drawable file cycle")
        size = archive.getinfo(value).file_size
        budget[0] -= size
        if budget[0] < 0:
            raise ValueError("drawable files too large")
        if value.endswith(apk.RASTER):
            png = apk._largest(archive, [value])
            if png is None:
                raise ValueError("invalid drawable bitmap")
            return ET.Element("image", width="108", height="108", href="data:image/png;base64," + base64.b64encode(png).decode())
        tag, attrs, children = _tree(archive.read(value))
        if tag == "vector":
            width, height = (_number(attrs[k]) for k in ("viewportWidth", "viewportHeight"))
            if not 0 < width <= 4096 or not 0 < height <= 4096:
                raise ValueError("invalid vector viewport")
            node = ET.Element("svg", width="108", height="108", viewBox=f"0 0 {width} {height}")
            group = ET.SubElement(node, "g", opacity=str(_number(attrs.get("alpha", 1))))
            vector_children(group, children)
            return node
        if tag == "adaptive-icon":
            layers = {t: a for t, a, _ in children}
            node = ET.Element("svg", width="108", height="108", viewBox="18 18 72 72")
            for layer in ("background", "foreground"):
                node.append(drawable(layers[layer]["drawable"], (*seen, value)))
            return node
        raise ValueError("unsupported drawable")

    root = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", width=str(SIZE), height=str(SIZE), viewBox="0 0 108 108")
    root.append(drawable(name))
    svg = ET.tostring(root, encoding="unicode")
    if len(svg) > MAX_SVG:
        raise ValueError("vector paths too large")
    return resvg_py.svg_to_bytes(svg_string=svg, width=SIZE, height=SIZE, skip_system_fonts=True)
