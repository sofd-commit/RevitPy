# Dynamo Python Script — Revit 2023 / IronPython 2.7
#
# Архитектура: диспетчер стратегий по категории (BuiltInCategory -> функция расчёта),
# вместо единой таблицы built-in кандидатов для всех категорий сразу.
#
# ПОРЯДОК РАСЧЁТА ДЛЯ КАЖДОГО ЭЛЕМЕНТА:
#   Шаг 0. Категорийная стратегия (если для категории элемента есть спец-обработка) —
#          использует built-in параметры/API-свойства, специфичные для этой категории.
#   Шаг 1. Общий built-in-фолбэк (BIP_CANDIDATES) — только для осей, которые
#          НЕ принадлежат категорийной стратегии и не помечены как "не применимо".
#   Шаг 2. BoundingBox / LocationCurve / реальная геометрия — только для осей,
#          не закреплённых за категорийной стратегией.
#
# ВАЖНО (фикс стабильности):
#   STRATEGY_OWNED_AXES — оси, которыми владеет категорийная стратегия.
#   Если стратегия есть, для этих осей НЕ выполняется generic built-in/bbox фолбэк,
#   даже если стратегия не нашла значение. Это устраняет подмену габаритов
#   (пример: у дверей/окон y = ширина вместо толщины).
#   Значения <= 0 не принимаются и не записываются.
#
# КАТЕГОРИЙНЫЕ СТРАТЕГИИ:
#
#   Doors, Windows (проёмы):
#     x = FAMILY_WIDTH_PARAM, z = FAMILY_HEIGHT_PARAM,
#     y = толщина (LookupParameter RU/EN + семейные синонимы).
#     Оси x/y/z закреплены за стратегией — без generic/bbox фолбэка.
#
#   DuctCurves, PipeCurves, CableTray, Conduit, FlexDuct, FlexPipe:
#     Круглое: x = z = диаметр. Прямоугольное: x = ширина, z = высота.
#     y = Н/П. l только по LocationCurve/built-in длины (без max(bbox)).
#
#   DuctFitting/PipeFitting/CableTrayFitting/ConduitFitting:
#     Габариты из локального bbox символа + размеры коннекторов (если есть).
#     Built-in Width/Height фитингов не используются (Revit может менять их местами).
#
#   Walls:
#     z = высота (WALL_USER_HEIGHT_PARAM -> оценка по уровням/локальному Z),
#     y = толщина (WALL_ATTR_WIDTH_PARAM / CompoundStructure).
#     x = Н/П. Curtain Wall исключены (составная система).
#     l только по LocationCurve/built-in длины (без max(bbox)).
#
#   Floors, Roofs, Ceilings:
#     z = CompoundStructure.GetWidth() -> built-in толщина типа.
#     x, y, l = Н/П.
#
#   Rooms, MEPSpaces, Areas:
#     s/v/l из SpatialElement.Area/Volume/Perimeter. x/y/z = Н/П.
#
#   StructuralColumns / StructuralFraming:
#     Колонны: z = длина оси; x/y = сечение ⊥ оси (геометрия / Diameter API).
#       Имена b/t/Ширина НЕ используются первыми (часто = толщина стенки, напр. 6 мм).
#     Балки/каркас: l = длина по оси;
#       x/z = сечение ГЕОМЕТРИЕЙ ⊥ оси (не имена параметров семейства).
#       Дополнительно: Structural Section Diameter/API, symbol bbox.
#       y = Н/П. World AABB не используется.
#
#   CurtainWallPanels / CurtainWallMullions:
#     Панели: x/z как ширина/высота семейства; импосты: l по кривой/длине.
#
#   Stairs, StairsRailing, Ramps, CurtainSystem:
#     Исключены (составная геометрия). Элемент -> skipped_unsupported.
#
#   Остальное (Furniture, Casework, Generic Models, Equipment и т.д.):
#     BIP_CANDIDATES -> локальный bbox символа.
#
# Параметры проекта: x/y/z/l (Длина), v (Объём), s (Площадь).
# Опционально IN[2]/IN[3]: копирование в текстовые ПИМ_Размер_*.

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')
from Autodesk.Revit.DB import *
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument

# Совместимость IronPython 2.7 / CPython 3 (Dynamo)
try:
    _STRING_TYPES = basestring  # noqa: F821 — IronPython / Py2
except NameError:
    _STRING_TYPES = (str,)

def is_text(value):
    return isinstance(value, _STRING_TYPES)

def as_bool(value, default=False):
    """Надёжное Да/Нет из портов Dynamo (bool / 0/1 / 'Да' / 'False')."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    try:
        s = unicode(value).strip().lower()  # IronPython
    except NameError:
        s = str(value).strip().lower()
    if s in (u"true", u"yes", u"да", u"1", u"y"):
        return True
    if s in (u"false", u"no", u"нет", u"0", u"n"):
        return False
    return default

def source_is_fallback(src):
    if not is_text(src):
        try:
            src = str(src)
        except:
            return False
    if src in ("bbox", "geometry", "bbox (max)"):
        return True
    try:
        return (u"local bbox" in src) or (u"санация" in src)
    except:
        return False

# ---------------------------------------------------------
# 1. Разворачиваем входные данные в плоский список элементов
# ---------------------------------------------------------

def flatten(lst):
    result = []
    for item in lst:
        if isinstance(item, list):
            result.extend(flatten(item))
        else:
            result.append(item)
    return result

raw_input = UnwrapElement(IN[0])
elements = flatten(raw_input) if isinstance(raw_input, list) else [raw_input]
elements = [el for el in elements if el is not None and hasattr(el, "Id")]

# ---------------------------------------------------------
# 1a. Фильтр по рабочему набору (IN[1], необязательный).
# ---------------------------------------------------------

def normalize_ws_name(s):
    if s is None:
        return u""
    return s.replace(u"\xa0", u" ").strip().lower()

workset_filter_raw = IN[1] if len(IN) > 1 else None
if workset_filter_raw is None or workset_filter_raw == "":
    workset_filter = None
elif isinstance(workset_filter_raw, list):
    workset_filter = set(normalize_ws_name(w) for w in workset_filter_raw if w)
else:
    workset_filter = set([normalize_ws_name(workset_filter_raw)])

def get_workset_name(el, doc):
    if not doc.IsWorkshared:
        return None
    try:
        ws_id = el.WorksetId
    except:
        return None
    if ws_id is None or ws_id == WorksetId.InvalidWorksetId:
        return None
    try:
        ws = doc.GetWorksetTable().GetWorkset(ws_id)
        return ws.Name
    except:
        return None

skipped_by_workset = []

if workset_filter is not None:
    filtered_elements = []
    for el in elements:
        ws_name = get_workset_name(el, doc)
        if ws_name is not None and normalize_ws_name(ws_name) in workset_filter:
            filtered_elements.append(el)
        else:
            skipped_by_workset.append(el.Id.IntegerValue)
    elements = filtered_elements

# ---------------------------------------------------------
# 2. Базовые хелперы
# ---------------------------------------------------------

def bip(name):
    return getattr(BuiltInParameter, name, None)

def catid(name):
    bc = getattr(BuiltInCategory, name, None)
    return int(bc) if bc is not None else None

def is_positive_number(val):
    try:
        return val is not None and float(val) > 0.0
    except:
        return False

def get_builtin_value(elem_or_type, bip_enum):
    """Возвращает (значение, имя_параметра), если параметр найден, заполнен, числовой и > 0."""
    if elem_or_type is None or bip_enum is None:
        return None
    try:
        p = elem_or_type.get_Parameter(bip_enum)
    except:
        return None
    if p is None or not p.HasValue:
        return None
    if p.StorageType != StorageType.Double:
        return None
    try:
        value = p.AsDouble()
    except:
        return None
    if not is_positive_number(value):
        return None
    try:
        name = p.Definition.Name
    except:
        name = str(bip_enum)
    return (value, name)

def get_val_inst_or_type(el, type_elem, bip_enum):
    found = get_builtin_value(el, bip_enum)
    if found is None and type_elem is not None:
        found = get_builtin_value(type_elem, bip_enum)
    return found

def get_lookup_double(el, type_elem, names):
    """Ищет double-параметр > 0 по списку имён: сначала экземпляр, потом тип."""
    for pname in names:
        p = None
        try:
            p = el.LookupParameter(pname)
        except:
            p = None
        if (p is None or not p.HasValue) and type_elem is not None:
            try:
                p = type_elem.LookupParameter(pname)
            except:
                p = None
        if p is None or not p.HasValue:
            continue
        if p.StorageType != StorageType.Double:
            continue
        try:
            value = p.AsDouble()
        except:
            continue
        if is_positive_number(value):
            return (value, u"семейство: {0}".format(pname))
    return None

def get_cat_id(el):
    try:
        return el.Category.Id.IntegerValue if el.Category is not None else None
    except:
        return None

def first_found(candidates):
    for found in candidates:
        if found is not None:
            return found
    return None

# ---------------------------------------------------------
# 3. Общая таблица built-in кандидатов (универсальный фолбэк)
# ---------------------------------------------------------

BIP_CANDIDATES = {
    "x": [bip(n) for n in [
        "GENERIC_DEPTH",
        "CASEWORK_DEPTH",
        "FURNITURE_DEPTH",
    ]],
    "y": [bip(n) for n in [
        "GENERIC_WIDTH",
        "FAMILY_WIDTH_PARAM",
        "INSTANCE_WIDTH_PARAM",
        "CASEWORK_WIDTH",
        "FURNITURE_WIDTH",
    ]],
    "z": [bip(n) for n in [
        "GENERIC_HEIGHT",
        "FAMILY_HEIGHT_PARAM",
        "INSTANCE_HEIGHT_PARAM",
        "CASEWORK_HEIGHT",
        "FURNITURE_HEIGHT",
    ]],
    "v": [bip(n) for n in [
        "HOST_VOLUME_COMPUTED",
    ]],
    "s": [bip(n) for n in [
        "HOST_AREA_COMPUTED",
    ]],
    "l": [bip(n) for n in [
        "CURVE_ELEM_LENGTH",
        "INSTANCE_LENGTH_PARAM",
        "STRUCTURAL_FRAME_CUT_LENGTH",
    ]],
}
for axis in BIP_CANDIDATES:
    BIP_CANDIDATES[axis] = [b for b in BIP_CANDIDATES[axis] if b is not None]

LINEAR_AXES = ("x", "y", "z")
SCALAR_AXES = ("v", "s")
LENGTH_AXIS = "l"
ALL_AXES = LINEAR_AXES + SCALAR_AXES + (LENGTH_AXIS,)

def find_existing_geometry_value(el, type_elem, axis):
    for bip_enum in BIP_CANDIDATES[axis]:
        found = get_val_inst_or_type(el, type_elem, bip_enum)
        if found is not None:
            return found
    return None

# ---------------------------------------------------------
# 4. Геометрические хелперы (нужны стратегиям фитингов/колонн)
# ---------------------------------------------------------

_GEOM_OPTIONS = Options()
_GEOM_OPTIONS.ComputeReferences = False
_GEOM_OPTIONS.IncludeNonVisibleObjects = False
try:
    _GEOM_OPTIONS.DetailLevel = ViewDetailLevel.Fine
except:
    pass

def get_bbox_dimensions(el):
    """Мировой (axis-aligned) bbox — используется, если локальный недоступен."""
    try:
        bb = el.get_BoundingBox(None)
    except:
        bb = None
    if bb is None:
        return None
    dx = abs(bb.Max.X - bb.Min.X)
    dy = abs(bb.Max.Y - bb.Min.Y)
    dz = abs(bb.Max.Z - bb.Min.Z)
    if not (is_positive_number(dx) or is_positive_number(dy) or is_positive_number(dz)):
        return None
    return {"x": dx, "y": dy, "z": dz}

def get_local_bbox_dimensions(el):
    """Bbox в ЛОКАЛЬНОЙ системе координат типа (GetSymbolGeometry)."""
    try:
        geom = el.get_Geometry(_GEOM_OPTIONS)
    except:
        geom = None

    if geom is not None:
        min_pt = [None, None, None]
        max_pt = [None, None, None]
        found = [False]

        def update(pt):
            coords = (pt.X, pt.Y, pt.Z)
            for i in range(3):
                v = coords[i]
                if min_pt[i] is None or v < min_pt[i]:
                    min_pt[i] = v
                if max_pt[i] is None or v > max_pt[i]:
                    max_pt[i] = v
            found[0] = True

        def walk_local(gobj_list):
            for gobj in gobj_list:
                if isinstance(gobj, Solid):
                    if gobj.Volume and gobj.Volume > 0:
                        for edge in gobj.Edges:
                            try:
                                curve = edge.AsCurve()
                                pts = curve.Tessellate()
                                for pt in pts:
                                    update(pt)
                            except:
                                continue
                elif isinstance(gobj, GeometryInstance):
                    try:
                        sym_geom = gobj.GetSymbolGeometry()
                    except:
                        sym_geom = None
                    if sym_geom is not None:
                        walk_local(sym_geom)

        walk_local(geom)

        if found[0]:
            dx = max_pt[0] - min_pt[0]
            dy = max_pt[1] - min_pt[1]
            dz = max_pt[2] - min_pt[2]
            return {"x": dx, "y": dy, "z": dz}

    return get_bbox_dimensions(el)

def get_curve_length(el):
    """Длина по LocationCurve (для дуг — длина дуги, не хорда)."""
    try:
        loc = el.Location
    except:
        return None
    if isinstance(loc, LocationCurve):
        try:
            length = loc.Curve.Length
            if is_positive_number(length):
                return length
        except:
            return None
    return None

def get_geometry_volume_area(el):
    """Сумма Solid.Volume и площадей граней (с разворотом GeometryInstance)."""
    try:
        geom = el.get_Geometry(_GEOM_OPTIONS)
    except:
        return (None, None)
    if geom is None:
        return (None, None)

    total_volume_holder = [0.0]
    total_area_holder = [0.0]
    found_solid_holder = [False]

    def process_solid(solid):
        v = solid.Volume
        if v is None or v <= 0:
            return
        total_volume_holder[0] += v
        for face in solid.Faces:
            total_area_holder[0] += face.Area
        found_solid_holder[0] = True

    def walk(geom_element):
        for gobj in geom_element:
            if isinstance(gobj, Solid):
                process_solid(gobj)
            elif isinstance(gobj, GeometryInstance):
                try:
                    inst_geom = gobj.GetInstanceGeometry()
                except:
                    inst_geom = None
                if inst_geom is not None:
                    walk(inst_geom)

    walk(geom)

    if not found_solid_holder[0]:
        return (None, None)
    return (total_volume_holder[0], total_area_holder[0])

def dims_to_axis_result(dims, source_label, axis_map=None):
    """Преобразует dict габаритов в result-стратегии. axis_map: {axis: dim_key}."""
    if dims is None:
        return {}
    if axis_map is None:
        axis_map = {"x": "x", "y": "y", "z": "z"}
    result = {}
    for axis, key in axis_map.items():
        val = dims.get(key)
        if is_positive_number(val):
            result[axis] = (val, source_label)
    return result

# ---------------------------------------------------------
# 5. Категории со спец-обработкой
# ---------------------------------------------------------

CAT = {
    "Doors": catid("OST_Doors"),
    "Windows": catid("OST_Windows"),
    "DuctCurves": catid("OST_DuctCurves"),
    "FlexDuctCurves": catid("OST_FlexDuctCurves"),
    "DuctFitting": catid("OST_DuctFitting"),
    "PipeCurves": catid("OST_PipeCurves"),
    "FlexPipeCurves": catid("OST_FlexPipeCurves"),
    "PipeFitting": catid("OST_PipeFitting"),
    "CableTray": catid("OST_CableTray"),
    "CableTrayFitting": catid("OST_CableTrayFitting"),
    "Conduit": catid("OST_Conduit"),
    "ConduitFitting": catid("OST_ConduitFitting"),
    "Walls": catid("OST_Walls"),
    "Floors": catid("OST_Floors"),
    "Roofs": catid("OST_Roofs"),
    "Ceilings": catid("OST_Ceilings"),
    "Rooms": catid("OST_Rooms"),
    "MEPSpaces": catid("OST_MEPSpaces"),
    "Areas": catid("OST_Areas"),
    "Stairs": catid("OST_Stairs"),
    "StairsRailing": catid("OST_StairsRailing"),
    "Ramps": catid("OST_Ramps"),
    "CurtainSystem": catid("OST_CurtainSystem"),
    "StructuralColumns": catid("OST_StructuralColumns"),
    "StructuralFraming": catid("OST_StructuralFraming"),
    "CurtainWallPanels": catid("OST_CurtainWallPanels"),
    "CurtainWallMullions": catid("OST_CurtainWallMullions"),
}

# --- 5a. Doors / Windows (проёмы) ---

DOOR_THICKNESS_PARAM_NAMES = [
    u"Толщина",
    u"Thickness",
    u"Door Thickness",
    u"Frame Thickness",
]
WINDOW_THICKNESS_PARAM_NAMES = [
    u"Толщина",
    u"Толщина коробки",
    u"Толщина рамы",
    u"Thickness",
    u"Frame Thickness",
    u"Window Thickness",
    u"Frame Depth",
]

SECTION_WIDTH_NAMES = [
    u"Ширина", u"Width", u"b", u"B", u"h1", u"bf",
]
SECTION_HEIGHT_NAMES = [
    u"Высота", u"Height", u"h", u"H", u"d", u"ht",
]
SECTION_DEPTH_NAMES = [
    u"Глубина", u"Depth", u"b", u"B", u"t",
]

def make_opening_strategy(thickness_names):
    def strategy(el, type_elem):
        result = {}
        w = get_val_inst_or_type(el, type_elem, bip("FAMILY_WIDTH_PARAM"))
        if w is None:
            w = get_lookup_double(el, type_elem, [u"Ширина", u"Width"])
        if w is not None:
            result["x"] = w

        h = get_val_inst_or_type(el, type_elem, bip("FAMILY_HEIGHT_PARAM"))
        if h is None:
            h = get_lookup_double(el, type_elem, [u"Высота", u"Height"])
        if h is not None:
            result["z"] = h

        th = get_lookup_double(el, type_elem, thickness_names)
        if th is not None:
            result["y"] = th
        return result
    return strategy

strategy_doors = make_opening_strategy(DOOR_THICKNESS_PARAM_NAMES)
strategy_windows = make_opening_strategy(WINDOW_THICKNESS_PARAM_NAMES)

OPENING_CAT_IDS = set([c for c in (CAT["Doors"], CAT["Windows"]) if c is not None])

# --- 5b. Прямые/гибкие MEP-участки ---

def make_mep_strategy(width_bip_name, height_bip_name, diameter_bip_name):
    d_bip = bip(diameter_bip_name) if diameter_bip_name else None
    w_bip = bip(width_bip_name) if width_bip_name else None
    h_bip = bip(height_bip_name) if height_bip_name else None

    def strategy(el, type_elem):
        result = {}
        if d_bip is not None:
            found = get_val_inst_or_type(el, type_elem, d_bip)
            if found is not None:
                result["x"] = found
                result["z"] = found
                return result
        if w_bip is not None:
            found = get_val_inst_or_type(el, type_elem, w_bip)
            if found is not None:
                result["x"] = found
        if h_bip is not None:
            found = get_val_inst_or_type(el, type_elem, h_bip)
            if found is not None:
                result["z"] = found
        return result
    return strategy

strategy_duct = make_mep_strategy("RBS_CURVE_WIDTH_PARAM", "RBS_CURVE_HEIGHT_PARAM", "RBS_CURVE_DIAMETER_PARAM")
strategy_pipe = make_mep_strategy(None, None, "RBS_PIPE_DIAMETER_PARAM")
strategy_cabletray = make_mep_strategy("RBS_CABLETRAY_WIDTH_PARAM", "RBS_CABLETRAY_HEIGHT_PARAM", None)
strategy_conduit = make_mep_strategy(None, None, "RBS_CONDUIT_DIAMETER_PARAM")
strategy_flex_duct = make_mep_strategy("RBS_CURVE_WIDTH_PARAM", "RBS_CURVE_HEIGHT_PARAM", "RBS_CURVE_DIAMETER_PARAM")
strategy_flex_pipe = make_mep_strategy(None, None, "RBS_PIPE_DIAMETER_PARAM")

# --- 5c. MEP fittings (коннекторы + локальный bbox, без Width/Height BIP) ---

def _connector_size_pair(connector):
    """Возвращает (width_or_diameter, height_or_None, kind) из коннектора."""
    try:
        shape = connector.Shape
    except:
        return None
    try:
        if shape == ConnectorProfileType.Round:
            d = connector.Radius * 2.0
            if is_positive_number(d):
                return (d, d, u"connector round")
        elif shape == ConnectorProfileType.Rectangular:
            w = connector.Width
            h = connector.Height
            if is_positive_number(w) and is_positive_number(h):
                return (w, h, u"connector rectangular")
        elif shape == ConnectorProfileType.Oval:
            w = connector.Width
            h = connector.Height
            if is_positive_number(w) and is_positive_number(h):
                return (w, h, u"connector oval")
    except:
        return None
    return None

def strategy_fitting(el, type_elem):
    """Фитинги: не используем built-in Width/Height (меняются местами).
    Берём max сечение по коннекторам + локальный bbox для недостающих осей."""
    result = {}

    conn_x = None
    conn_z = None
    conn_src = None
    try:
        cm = el.ConnectorManager
        if cm is not None:
            for connector in cm.Connectors:
                pair = _connector_size_pair(connector)
                if pair is None:
                    continue
                w, h, kind = pair
                if conn_x is None or w > conn_x:
                    conn_x = w
                    conn_src = kind
                if conn_z is None or h > conn_z:
                    conn_z = h
                    conn_src = kind
    except:
        pass

    if is_positive_number(conn_x):
        result["x"] = (conn_x, conn_src or u"connector")
    if is_positive_number(conn_z):
        result["z"] = (conn_z, conn_src or u"connector")

    dims = get_local_bbox_dimensions(el)
    if dims is not None:
        # Для фитинга: недостающие линейные оси — из локального bbox.
        # y часто = «глубина» вдоль потока: берём наибольший из оставшихся габаритов,
        # если ещё не заполнен через коннекторы.
        for axis in ("x", "y", "z"):
            if axis in result:
                continue
            val = dims.get(axis)
            if is_positive_number(val):
                result[axis] = (val, u"local bbox (fitting)")

        # Длина фитинга — максимальный габарит локального bbox как envelope-длина
        # только если LocationCurve позже не даст значение (owned l нет).
        # Здесь l не заполняем — пусть curve/built-in длины работают как обычно.

    return result

# --- 5d. Walls ---

def is_curtain_wall(el, type_elem):
    try:
        wt = type_elem if type_elem is not None else None
        if wt is None:
            try:
                wt = el.WallType
            except:
                wt = None
        if wt is not None and hasattr(wt, "Kind"):
            return wt.Kind == WallKind.Curtain
    except:
        pass
    return False

def estimate_wall_height(el):
    """Оценка высоты стены, если WALL_USER_HEIGHT_PARAM пуст (присоединение к уровню и т.п.)."""
    dims = get_local_bbox_dimensions(el)
    if dims is not None and is_positive_number(dims.get("z")):
        return (dims["z"], u"bbox Z (оценка высоты стены)")
    return None

def strategy_walls(el, type_elem):
    result = {}
    if is_curtain_wall(el, type_elem):
        # Обрабатывается как unsupported на уровне цикла (см. ниже), сюда лучше не попадать.
        return result

    h = get_val_inst_or_type(el, type_elem, bip("WALL_USER_HEIGHT_PARAM"))
    if h is None:
        h = estimate_wall_height(el)
    if h is not None:
        result["z"] = h

    w = get_val_inst_or_type(el, type_elem, bip("WALL_ATTR_WIDTH_PARAM"))
    if w is None and type_elem is not None:
        try:
            cs = type_elem.GetCompoundStructure()
            if cs is not None:
                thickness = cs.GetWidth()
                if is_positive_number(thickness):
                    w = (thickness, u"CompoundStructure (толщина типа стены)")
        except:
            pass
    if w is not None:
        result["y"] = w
    return result

# --- 5e. Floors / Roofs / Ceilings ---

PLANAR_THICKNESS_BIPS = {
    "Floors": ["FLOOR_ATTR_DEFAULT_THICKNESS_PARAM", "FLOOR_ATTR_THICKNESS_PARAM"],
    "Roofs": ["ROOF_ATTR_DEFAULT_THICKNESS_PARAM", "ROOF_ATTR_THICKNESS_PARAM"],
    "Ceilings": ["CEILING_THICKNESS", "CEILING_THICKNESS_PARAM"],
}

def make_planar_host_strategy(thickness_bip_names):
    def strategy(el, type_elem):
        result = {}
        if type_elem is not None:
            try:
                cs = type_elem.GetCompoundStructure()
            except:
                cs = None
            if cs is not None:
                try:
                    thickness = cs.GetWidth()
                    if is_positive_number(thickness):
                        result["z"] = (thickness, u"CompoundStructure (общая толщина типа)")
                        return result
                except:
                    pass

        for bip_name in thickness_bip_names:
            found = get_val_inst_or_type(el, type_elem, bip(bip_name))
            if found is not None:
                result["z"] = found
                break
        if "z" not in result:
            th = get_lookup_double(el, type_elem, [u"Толщина", u"Thickness", u"Default Thickness"])
            if th is not None:
                result["z"] = th
        return result
    return strategy

strategy_floors = make_planar_host_strategy(PLANAR_THICKNESS_BIPS["Floors"])
strategy_roofs = make_planar_host_strategy(PLANAR_THICKNESS_BIPS["Roofs"])
strategy_ceilings = make_planar_host_strategy(PLANAR_THICKNESS_BIPS["Ceilings"])

# --- 5f. Rooms / MEPSpaces / Areas ---

def strategy_spatial(el, type_elem):
    result = {}
    try:
        area = el.Area
        if is_positive_number(area):
            result["s"] = (area, u"SpatialElement.Area")
    except:
        pass
    try:
        vol = el.Volume
        if is_positive_number(vol):
            result["v"] = (vol, u"SpatialElement.Volume")
    except:
        pass
    try:
        perim = el.Perimeter
        if is_positive_number(perim):
            result["l"] = (perim, u"SpatialElement.Perimeter")
    except:
        pass
    return result

# --- 5g. Structural Columns / Framing ---

def strategy_structural_column(el, type_elem):
    """Несущие колонны — без имён параметров семейства (b/t часто = толщина стенки).

    z = высота/длина вдоль оси колонны
    x, y = сечение ⊥ оси (геометрия / Structural Section Diameter)
    """
    result = {}

    length_val = element_axis_length(el)
    # высота колонны
    h = None
    if length_val is not None:
        h = (length_val, u"LocationCurve (длина оси)")
    if h is None:
        h = first_found([
            get_val_inst_or_type(el, type_elem, bip("FAMILY_HEIGHT_PARAM")),
            get_val_inst_or_type(el, type_elem, bip("INSTANCE_LENGTH_PARAM")),
            get_val_inst_or_type(el, type_elem, bip("INSTANCE_HEIGHT_PARAM")),
        ])
    if h is not None and is_plausible_beam_section(h[0], None, trusted=True):
        result["z"] = h
        length_val = h[0] if length_val is None else length_val

    # 1) Диаметр Structural Section
    diam = get_beam_diameter_from_api(type_elem)
    if diam is not None and is_plausible_beam_section(diam[0], length_val, trusted=True):
        result["x"] = (diam[0], diam[1])
        result["y"] = (diam[0], diam[1])
        if "z" not in result:
            # высота из bbox вдоль оси / уже выше
            pass
        return result

    # 2) Геометрия ⊥ оси колонны
    axis = element_axis_vector(el)
    a, b, src = section_extents_perp_to_axis_vector(
        el, axis, length_val, u"геометрия ⊥ оси колонны"
    )
    width = (a, src) if a is not None else None
    height = (b, src) if b is not None else None
    width, height = normalize_thickness_vs_outer(width, height, length_val)

    # 3) Symbol bbox (без world)
    if width is None or height is None:
        dims = get_local_bbox_only(el)
        if dims is not None:
            # две меньшие стороны — сечение, наибольшая — высота (если z ещё нет)
            items = sorted(
                [(k, float(dims[k])) for k in ("x", "y", "z") if is_positive_number(dims.get(k))],
                key=lambda kv: kv[1]
            )
            if len(items) >= 2:
                if width is None and is_plausible_beam_section(items[0][1], length_val, trusted=True):
                    width = (items[0][1], u"symbol bbox (сечение)")
                if height is None and is_plausible_beam_section(items[1][1], length_val, trusted=True):
                    height = (items[1][1], u"symbol bbox (сечение)")
            if "z" not in result and len(items) >= 3:
                result["z"] = (items[2][1], u"symbol bbox (высота)")
                length_val = items[2][1]
        width, height = normalize_thickness_vs_outer(width, height, length_val)

    # 4) Structural Section W/H API
    if width is None or height is None:
        api_w, api_h, api_src = get_beam_section_from_structural_api(type_elem)
        if width is None and api_w is not None and is_plausible_beam_section(api_w, length_val, trusted=True):
            width = (api_w, api_src)
        if height is None and api_h is not None and is_plausible_beam_section(api_h, length_val, trusted=True):
            height = (api_h, api_src)
        width, height = normalize_thickness_vs_outer(width, height, length_val)

    # Круглое сечение
    if width is not None and height is None:
        height = (width[0], u"колонна: y=x")
    elif height is not None and width is None:
        width = (height[0], u"колонна: x=y")
    elif width is not None and height is not None:
        try:
            if nearly_equal_len(width[0], height[0], rel=0.08):
                mid = 0.5 * (float(width[0]) + float(height[0]))
                width = (mid, width[1])
                height = (mid, height[1])
        except:
            pass

    if width is not None:
        result["x"] = width
    if height is not None:
        result["y"] = height

    if "z" not in result:
        dims = get_local_bbox_only(el)
        if dims is not None and is_positive_number(dims.get("z")):
            result["z"] = (dims["z"], u"symbol bbox Z")

    return result

def nearly_equal_len(a, b, rel=0.02):
    """Сравнение длин с допуском (~2%)."""
    try:
        if a is None or b is None:
            return False
        a = float(a)
        b = float(b)
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) <= rel * max(a, b)
    except:
        return False

def is_plausible_beam_section(val, length_val, trusted=False, peer=None):
    """Отсекает длину/пролёт, попавшие в сечение.
    trusted=True — значение из Structural Section / явного b,h типа.
    Без trusted: сечение не больше ~20% длины и не в разы больше второй стороны."""
    if not is_positive_number(val):
        return False
    try:
        val = float(val)
    except:
        return False
    if length_val is not None:
        try:
            lv = float(length_val)
        except:
            lv = None
        if lv is not None and lv > 0:
            if nearly_equal_len(val, lv):
                return False
            # 2260 при l=5880 (~38%) — это пролёт по Z, не сечение 120
            limit = 0.95 if trusted else 0.20
            if val >= lv * limit:
                return False
    if (not trusted) and peer is not None and is_positive_number(peer):
        try:
            peer = float(peer)
            # Явно непропорционально (2260 vs 128) — отбрасываем большую сторону
            if val > peer * 4.0 and val > peer + 1e-6:
                return False
        except:
            pass
    return True

def get_lookup_double_type_first(el, type_elem, names):
    """Сначала ТИП, потом экземпляр — для сечения балки тип надёжнее."""
    for pname in names:
        for host in (type_elem, el):
            if host is None:
                continue
            try:
                p = host.LookupParameter(pname)
            except:
                p = None
            if p is None or not p.HasValue:
                continue
            if p.StorageType != StorageType.Double:
                continue
            try:
                value = p.AsDouble()
            except:
                continue
            if is_positive_number(value):
                where = u"тип" if host is type_elem else u"экземпляр"
                return (value, u"{0}: {1}".format(where, pname))
    return None

def get_local_bbox_only(el):
    """Только локальный bbox символа. БЕЗ фолбэка на мировой bbox
    (мировой Z у балки/связи = пролёт по высоте, напр. 2260 вместо 120)."""
    try:
        geom = el.get_Geometry(_GEOM_OPTIONS)
    except:
        return None
    if geom is None:
        return None

    min_pt = [None, None, None]
    max_pt = [None, None, None]
    found = [False]

    def update(pt):
        coords = (pt.X, pt.Y, pt.Z)
        for i in range(3):
            v = coords[i]
            if min_pt[i] is None or v < min_pt[i]:
                min_pt[i] = v
            if max_pt[i] is None or v > max_pt[i]:
                max_pt[i] = v
        found[0] = True

    def walk_local(gobj_list):
        for gobj in gobj_list:
            if isinstance(gobj, Solid):
                if gobj.Volume and gobj.Volume > 0:
                    for edge in gobj.Edges:
                        try:
                            curve = edge.AsCurve()
                            pts = curve.Tessellate()
                            for pt in pts:
                                update(pt)
                        except:
                            continue
            elif isinstance(gobj, GeometryInstance):
                try:
                    sym_geom = gobj.GetSymbolGeometry()
                except:
                    sym_geom = None
                if sym_geom is not None:
                    walk_local(sym_geom)

    walk_local(geom)
    if not found[0]:
        return None
    return {
        "x": max_pt[0] - min_pt[0],
        "y": max_pt[1] - min_pt[1],
        "z": max_pt[2] - min_pt[2],
    }

def get_beam_diameter_from_api(type_elem):
    """Наружный диаметр трубы/круглого сечения (если есть)."""
    if type_elem is None:
        return None
    d_bip = first_found([
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_COMMON_DIAMETER")),
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_COMMON_OUTER_DIAMETER")),
    ])
    if d_bip is not None and is_positive_number(d_bip[0]):
        return (d_bip[0], u"StructuralSection diameter: {0}".format(d_bip[1]))

    ss = None
    try:
        fam = type_elem.Family
        can = True
        try:
            can = fam.CanHaveStructuralSection()
        except:
            can = True
        if can:
            ss = type_elem.GetStructuralSection()
    except:
        ss = None
    if ss is not None:
        for attr in ("Diameter", "OuterDiameter"):
            try:
                dv = getattr(ss, attr)
                if is_positive_number(dv):
                    return (dv, u"StructuralSection.{0}".format(attr))
            except:
                continue
    return None

def get_beam_section_from_structural_api(type_elem):
    """Ширина/высота из Structural Section.
    Для труб сначала диаметр (x=z=D), иначе Width может оказаться толщиной стенки."""
    if type_elem is None:
        return (None, None, None)

    diam = get_beam_diameter_from_api(type_elem)
    if diam is not None:
        return (diam[0], diam[0], diam[1])

    width = None
    height = None
    src_parts = []

    w_bip = first_found([
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_COMMON_WIDTH")),
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_WIDTH")),
    ])
    h_bip = first_found([
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_COMMON_HEIGHT")),
        get_builtin_value(type_elem, bip("STRUCTURAL_SECTION_HEIGHT")),
    ])
    if w_bip is not None:
        width = w_bip[0]
        src_parts.append(w_bip[1])
    if h_bip is not None:
        height = h_bip[0]
        src_parts.append(h_bip[1])

    if width is None or height is None:
        ss = None
        try:
            fam = type_elem.Family
            can = True
            try:
                can = fam.CanHaveStructuralSection()
            except:
                can = True
            if can:
                ss = type_elem.GetStructuralSection()
        except:
            ss = None
        if ss is not None:
            if width is None:
                try:
                    wv = ss.Width
                    if is_positive_number(wv):
                        width = wv
                        src_parts.append(u"StructuralSection.Width")
                except:
                    pass
            if height is None:
                try:
                    hv = ss.Height
                    if is_positive_number(hv):
                        height = hv
                        src_parts.append(u"StructuralSection.Height")
                except:
                    pass

    if width is None and height is None:
        return (None, None, None)
    return (width, height, u"struct: {0}".format(u", ".join(src_parts)))

# Имена сечения типа (в т.ч. ADSK / СПДС). Только с типа.
BEAM_DIAMETER_PARAM_NAMES = [
    u"Диаметр", u"D", u"Ø", u"DN",
    u"Наружный диаметр", u"Внешний диаметр", u"dнар", u"Dнар", u"Dн",
    u"Diameter", u"Outer Diameter",
    u"ADSK_Диаметр", u"ADSK_Размер_Диаметр",
]
# b/B/bf НЕ в ширине по умолчанию для труб: у трубы b часто = толщина стенки (4 мм).
# Для прямоугольных сечений b подхватит scan / Structural Section / геометрия.
BEAM_WIDTH_PARAM_NAMES = [
    u"Ширина", u"Ширина сечения", u"Ширина полки", u"Ширина профиля",
    u"ADSK_Размер_Ширина", u"ADSK_Ширина", u"Рзм.Ширина",
]
BEAM_WIDTH_PARAM_NAMES_RECT = [
    u"b", u"bf", u"B", u"b_фл",
] + BEAM_WIDTH_PARAM_NAMES
BEAM_HEIGHT_PARAM_NAMES = [
    u"h", u"H", u"ht",
    u"Высота", u"Высота сечения", u"Высота профиля", u"Высота балки",
    u"ADSK_Размер_Высота", u"ADSK_Высота", u"Рзм.Высота",
]
THICKNESS_NAME_TOKENS = (
    u"толщ", u"thick", u"стенк", u"wall", u"wt", u"tстен", u"t_стен",
)

def is_pipe_like_framing(el, type_elem):
    """Труба / круглое сечение по имени семейства/типа."""
    parts = []
    for obj, attr in (
        (type_elem, "FamilyName"),
        (type_elem, "Name"),
        (el, "Name"),
    ):
        if obj is None:
            continue
        try:
            parts.append(getattr(obj, attr))
        except:
            pass
    try:
        if type_elem is not None:
            p = type_elem.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
            if p is not None and p.HasValue:
                parts.append(p.AsString())
    except:
        pass
    # Structural section shape
    try:
        if type_elem is not None:
            ss = type_elem.GetStructuralSection()
            if ss is not None:
                shape = str(ss.StructuralSectionShape)
                parts.append(shape)
    except:
        pass
    blob = u" ".join([p for p in parts if p]).lower()
    tokens = (
        u"труб", u"pipe", u"tube", u"круг", u"round", u"hollow",
        u"кольц", u"circular", u"pipestandard", u"roundhss",
    )
    return any(t in blob for t in tokens)

def looks_like_wall_thickness(small_val, large_val):
    """4 мм при наружном 60 мм: small << large (толщина стенки vs габарит)."""
    try:
        s = float(small_val)
        L = float(large_val)
        if s <= 0 or L <= 0:
            return False
        return (s < L * 0.25) and (L / s >= 5.0)
    except:
        return False

def normalize_thickness_vs_outer(width, height, length_val):
    """Если одна сторона похожа на толщину стенки — обе = наружный размер."""
    if width is None or height is None:
        return (width, height)
    w, h = width[0], height[0]
    if looks_like_wall_thickness(w, h):
        src = u"наружный размер (b/ширина={0} отброшена как толщина стенки)".format(w)
        return ((h, src), (h, height[1] if height[1] else src))
    if looks_like_wall_thickness(h, w):
        src = u"наружный размер (высота={0} отброшена как толщина стенки)".format(h)
        return ((w, width[1] if width[1] else src), (w, src))
    return (width, height)

def get_lookup_double_type_only(type_elem, names):
    """Только параметр ТИПА — без экземпляра."""
    if type_elem is None:
        return None
    for pname in names:
        try:
            p = type_elem.LookupParameter(pname)
        except:
            p = None
        if p is None or not p.HasValue:
            continue
        if p.StorageType != StorageType.Double:
            continue
        try:
            value = p.AsDouble()
        except:
            continue
        if is_positive_number(value):
            return (value, u"тип: {0}".format(pname))
    return None

def scan_type_section_by_name(type_elem, length_val):
    """Запасной поиск сечения по именам параметров типа (ширин*/высот*/b/h)."""
    if type_elem is None:
        return (None, None)
    width = None
    height = None
    try:
        params = type_elem.Parameters
    except:
        return (None, None)
    for p in params:
        try:
            if p.StorageType != StorageType.Double or not p.HasValue:
                continue
            name = p.Definition.Name
            val = p.AsDouble()
        except:
            continue
        if not is_plausible_beam_section(val, length_val, trusted=True):
            continue
        try:
            nlow = name.lower()
        except:
            nlow = u""
        # отсечь длину/смещения/уклон
        skip_tokens = (u"длин", u"length", u"пролет", u"пролёт", u"offset", u"смещ",
                       u"уклон", u"angle", u"вес", u"weight", u"area", u"объём", u"объем", u"volume")
        if any(t in nlow for t in skip_tokens):
            continue
        if any(t in nlow for t in THICKNESS_NAME_TOKENS):
            continue
        # одиночная «b» у трубы = толщина — не берём как ширину в scan
        is_w = (u"ширин" in nlow) or (u"width" in nlow) or (u"полк" in nlow) or (nlow == u"bf")
        is_h = (nlow in (u"h", u"ht")) or (u"высот" in nlow) or (u"height" in nlow)
        is_d = (nlow in (u"d", u"dn")) or (u"диаметр" in nlow) or (u"diameter" in nlow)
        if is_d and width is None and height is None:
            # круглое: обе стороны = диаметр
            return ((val, u"тип scan diam: {0}".format(name)),
                    (val, u"тип scan diam: {0}".format(name)))
        if is_w and width is None:
            width = (val, u"тип scan: {0}".format(name))
        elif is_h and height is None:
            height = (val, u"тип scan: {0}".format(name))
        if width is not None and height is not None:
            break
    return (width, height)

def _xyz_norm(v):
    try:
        return v.Normalize()
    except:
        try:
            L = (v.X * v.X + v.Y * v.Y + v.Z * v.Z) ** 0.5
            if L < 1e-12:
                return None
            return XYZ(v.X / L, v.Y / L, v.Z / L)
        except:
            return None

def section_extents_perp_to_axis_vector(el, tangent, length_val, src_label):
    """Два габарита сечения в плоскости, перпендикулярной tangent (из instance solids)."""
    tangent = _xyz_norm(tangent)
    if tangent is None:
        return (None, None, None)

    world_up = XYZ(0.0, 0.0, 1.0)
    try:
        if abs(tangent.DotProduct(world_up)) > 0.99:
            world_up = XYZ(0.0, 1.0, 0.0)
        lateral = _xyz_norm(world_up.CrossProduct(tangent))
        if lateral is None:
            return (None, None, None)
        upright = _xyz_norm(tangent.CrossProduct(lateral))
        if upright is None:
            return (None, None, None)
    except:
        return (None, None, None)

    try:
        geom = el.get_Geometry(_GEOM_OPTIONS)
    except:
        return (None, None, None)
    if geom is None:
        return (None, None, None)

    min_l = [None]
    max_l = [None]
    min_u = [None]
    max_u = [None]
    found = [False]

    def accumulate(pt):
        ly = pt.X * lateral.X + pt.Y * lateral.Y + pt.Z * lateral.Z
        uz = pt.X * upright.X + pt.Y * upright.Y + pt.Z * upright.Z
        if min_l[0] is None or ly < min_l[0]:
            min_l[0] = ly
        if max_l[0] is None or ly > max_l[0]:
            max_l[0] = ly
        if min_u[0] is None or uz < min_u[0]:
            min_u[0] = uz
        if max_u[0] is None or uz > max_u[0]:
            max_u[0] = uz
        found[0] = True

    def walk(glist):
        for gobj in glist:
            if isinstance(gobj, Solid):
                if gobj.Volume and gobj.Volume > 0:
                    for edge in gobj.Edges:
                        try:
                            for pt in edge.AsCurve().Tessellate():
                                accumulate(pt)
                        except:
                            continue
            elif isinstance(gobj, GeometryInstance):
                try:
                    inst = gobj.GetInstanceGeometry()
                except:
                    inst = None
                if inst is not None:
                    walk(inst)

    walk(geom)
    if not found[0]:
        return (None, None, None)

    a = max_l[0] - min_l[0]
    b = max_u[0] - min_u[0]
    if not is_plausible_beam_section(a, length_val, trusted=True):
        a = None
    if not is_plausible_beam_section(b, length_val, trusted=True):
        b = None
    if a is None and b is None:
        return (None, None, None)
    return (a, b, src_label)

def element_axis_vector(el):
    """Ось линейного элемента: LocationCurve.tangent или BasisZ трансформации."""
    try:
        loc = el.Location
    except:
        loc = None
    if isinstance(loc, LocationCurve):
        try:
            return _xyz_norm(loc.Curve.ComputeDerivatives(0.5, True).BasisX)
        except:
            pass
    try:
        return _xyz_norm(el.GetTransform().BasisZ)
    except:
        pass
    return XYZ(0.0, 0.0, 1.0)

def element_axis_length(el):
    """Длина вдоль оси (кривая) или None."""
    try:
        loc = el.Location
    except:
        return None
    if isinstance(loc, LocationCurve):
        try:
            length = loc.Curve.Length
            if is_positive_number(length):
                return length
        except:
            return None
    return None

def framing_section_perp_to_axis(el, length_val):
    """Сечение балки/связи ⊥ LocationCurve (или оси трансформации)."""
    try:
        loc = el.Location
    except:
        loc = None
    if isinstance(loc, LocationCurve):
        try:
            tangent = _xyz_norm(loc.Curve.ComputeDerivatives(0.5, True).BasisX)
        except:
            tangent = None
        if tangent is None:
            return (None, None, None)
        return section_extents_perp_to_axis_vector(
            el, tangent, length_val, u"геометрия ⊥ оси кривой"
        )
    axis = element_axis_vector(el)
    return section_extents_perp_to_axis_vector(
        el, axis, length_val, u"геометрия ⊥ оси трансформации"
    )

def framing_section_from_bbox(el, length_val):
    """Запасной путь: только symbol bbox (без world AABB)."""
    dims = get_local_bbox_only(el)
    if dims is None:
        return (None, None, None)

    items = [(k, float(dims[k])) for k in ("x", "y", "z") if is_positive_number(dims.get(k))]
    if len(items) < 2:
        return (None, None, None)

    length_key = None
    if length_val is not None:
        for k, v in items:
            if nearly_equal_len(v, length_val) or v >= float(length_val) * 0.95:
                length_key = k
                break
    if length_key is None and len(items) == 3:
        ordered = sorted(items, key=lambda kv: kv[1])
        if ordered[2][1] > ordered[1][1] * 2.0:
            length_key = ordered[2][0]

    section_items = [(k, v) for k, v in items if k != length_key]
    if len(section_items) < 2 and length_key is None and len(items) == 3:
        max_dim = max(v for _, v in items)
        if length_val is None or max_dim < float(length_val) * 0.5:
            section_items = sorted(items, key=lambda kv: kv[1])[1:]

    if len(section_items) < 2:
        return (None, None, None)

    smap = dict(section_items)
    if "y" in smap and "z" in smap:
        width, height = smap["y"], smap["z"]
        src = u"symbol bbox (Y→x, Z→z)"
    elif "x" in smap and "z" in smap:
        width, height = smap["x"], smap["z"]
        src = u"symbol bbox (X→x, Z→z)"
    elif "x" in smap and "y" in smap:
        width, height = smap["x"], smap["y"]
        src = u"symbol bbox (X→x, Y→z)"
    else:
        ordered_vals = sorted(v for _, v in section_items)
        width, height = ordered_vals[0], ordered_vals[-1]
        src = u"symbol bbox (min/max)"

    if not is_plausible_beam_section(width, length_val, trusted=False, peer=height):
        width = None
    if not is_plausible_beam_section(height, length_val, trusted=False, peer=width):
        height = None
    if width is None and height is None:
        return (None, None, None)
    return (width, height, src)

def resolve_framing_section(el, type_elem, length_val):
    """Сечение каркаса БЕЗ опоры на имена параметров семейства
    (у разных семейств b/h/Ширина означают разное — вплоть до толщины стенки).

    Порядок:
      1) Structural Section API: Diameter (системный, не имя семейства)
      2) Геометрия в плоскости ⊥ оси LocationCurve  ← основной метод
      3) Symbol bbox (локальный, без world AABB)
      4) Structural Section Width/Height API
      5) Имена параметров типа — только крайний запасной путь
      + если одна сторона << другой (4 vs 60) — обе = наружный габарит
    """
    width = None
    height = None

    # 1) Диаметр из системного API Revit (не Lookup по имени семейства)
    diam = get_beam_diameter_from_api(type_elem)
    if diam is not None and is_plausible_beam_section(diam[0], length_val, trusted=True):
        return ((diam[0], diam[1]), (diam[0], diam[1]))

    # 2) Геометрия ⊥ оси — главный универсальный источник
    bw, bh, bsrc = framing_section_perp_to_axis(el, length_val)
    if bw is not None and is_plausible_beam_section(bw, length_val, trusted=True):
        width = (bw, bsrc)
    if bh is not None and is_plausible_beam_section(bh, length_val, trusted=True):
        height = (bh, bsrc)
    width, height = normalize_thickness_vs_outer(width, height, length_val)

    # 3) Symbol bbox
    if width is None or height is None:
        sw, sh, ssrc = framing_section_from_bbox(el, length_val)
        if width is None and sw is not None:
            width = (sw, ssrc)
        if height is None and sh is not None:
            height = (sh, ssrc)
        width, height = normalize_thickness_vs_outer(width, height, length_val)

    # 4) Structural Section Width/Height (системный API; для труб Diameter уже выше)
    if width is None or height is None:
        api_w, api_h, api_src = get_beam_section_from_structural_api(type_elem)
        if width is None and api_w is not None and is_plausible_beam_section(api_w, length_val, trusted=True):
            width = (api_w, api_src)
        if height is None and api_h is not None and is_plausible_beam_section(api_h, length_val, trusted=True):
            height = (api_h, api_src)
        width, height = normalize_thickness_vs_outer(width, height, length_val)

    # 5) Имена параметров — только если геометрия/API не дали сечения
    if width is None or height is None:
        pw = get_lookup_double_type_only(type_elem, BEAM_WIDTH_PARAM_NAMES_RECT)
        ph = get_lookup_double_type_only(type_elem, BEAM_HEIGHT_PARAM_NAMES)
        if width is None and pw is not None and is_plausible_beam_section(pw[0], length_val, trusted=True):
            width = pw
        if height is None and ph is not None and is_plausible_beam_section(ph[0], length_val, trusted=True):
            height = ph
        # не подставляем b=толщину, если вторая сторона уже есть как наружный размер
        width, height = normalize_thickness_vs_outer(width, height, length_val)

    # Круглое: стороны почти равны / одна отсутствует — выровнять
    if width is not None and height is None:
        height = (width[0], u"x=z (одна сторона сечения)")
    elif height is not None and width is None:
        width = (height[0], u"x=z (одна сторона сечения)")
    elif width is not None and height is not None:
        try:
            w, h = float(width[0]), float(height[0])
            if nearly_equal_len(w, h, rel=0.08):
                mid = 0.5 * (w + h)
                width = (mid, width[1])
                height = (mid, height[1])
        except:
            pass

    if width is not None and not is_plausible_beam_section(width[0], length_val, trusted=True):
        width = None
    if height is not None and not is_plausible_beam_section(height[0], length_val, trusted=True):
        height = None

    return (width, height)

def strategy_structural_framing(el, type_elem):
    """Балки/связи каркаса:
    l = длина по оси,
    x = ширина сечения,
    z = высота сечения,
    y = Н/П.
    """
    result = {}

    length = first_found([
        get_val_inst_or_type(el, type_elem, bip("STRUCTURAL_FRAME_CUT_LENGTH")),
        get_val_inst_or_type(el, type_elem, bip("INSTANCE_LENGTH_PARAM")),
        get_val_inst_or_type(el, type_elem, bip("CURVE_ELEM_LENGTH")),
    ])
    if length is None:
        curve_len = get_curve_length(el)
        if curve_len is not None:
            length = (curve_len, u"LocationCurve")
    length_val = length[0] if length is not None else None
    if length is not None:
        result["l"] = length

    width, height = resolve_framing_section(el, type_elem, length_val)
    if width is not None:
        result["x"] = width
    if height is not None:
        result["z"] = height

    return result

# --- 5h. Curtain panels / mullions ---

def strategy_curtain_panel(el, type_elem):
    """Панели: Width/Height BIP, затем локальный bbox — без произвольных имён семейства."""
    result = {}
    w = get_val_inst_or_type(el, type_elem, bip("FAMILY_WIDTH_PARAM"))
    h = get_val_inst_or_type(el, type_elem, bip("FAMILY_HEIGHT_PARAM"))
    if w is not None:
        result["x"] = w
    if h is not None:
        result["z"] = h
    if "x" not in result or "z" not in result:
        dims = get_local_bbox_only(el)
        if dims is None:
            dims = get_local_bbox_dimensions(el)
        if dims is not None:
            if "x" not in result and is_positive_number(dims.get("x")):
                result["x"] = (dims["x"], u"local bbox (panel)")
            if "z" not in result and is_positive_number(dims.get("z")):
                result["z"] = (dims["z"], u"local bbox (panel)")
            if "y" not in result and is_positive_number(dims.get("y")):
                result["y"] = (dims["y"], u"local bbox (panel thickness)")
    return result

def strategy_curtain_mullion(el, type_elem):
    result = {}
    length = first_found([
        get_val_inst_or_type(el, type_elem, bip("CURVE_ELEM_LENGTH")),
        get_val_inst_or_type(el, type_elem, bip("INSTANCE_LENGTH_PARAM")),
    ])
    if length is None:
        curve_len = get_curve_length(el)
        if curve_len is not None:
            length = (curve_len, u"LocationCurve")
    if length is not None:
        result["l"] = length

    dims = get_local_bbox_dimensions(el)
    if dims is not None:
        # Две меньшие стороны — сечение импоста
        vals = sorted([(k, dims[k]) for k in ("x", "y", "z") if is_positive_number(dims.get(k))],
                      key=lambda kv: kv[1])
        if len(vals) >= 2:
            result["x"] = (vals[0][1], u"local bbox (mullion section)")
            result["y"] = (vals[1][1], u"local bbox (mullion section)")
    return result

# ---------------------------------------------------------
# 6. Таблицы диспетчеризации
# ---------------------------------------------------------

STRATEGIES = {}
if CAT["Doors"] is not None:
    STRATEGIES[CAT["Doors"]] = strategy_doors
if CAT["Windows"] is not None:
    STRATEGIES[CAT["Windows"]] = strategy_windows
if CAT["DuctCurves"] is not None:
    STRATEGIES[CAT["DuctCurves"]] = strategy_duct
if CAT["FlexDuctCurves"] is not None:
    STRATEGIES[CAT["FlexDuctCurves"]] = strategy_flex_duct
if CAT["PipeCurves"] is not None:
    STRATEGIES[CAT["PipeCurves"]] = strategy_pipe
if CAT["FlexPipeCurves"] is not None:
    STRATEGIES[CAT["FlexPipeCurves"]] = strategy_flex_pipe
if CAT["CableTray"] is not None:
    STRATEGIES[CAT["CableTray"]] = strategy_cabletray
if CAT["Conduit"] is not None:
    STRATEGIES[CAT["Conduit"]] = strategy_conduit
for _key in ("DuctFitting", "PipeFitting", "CableTrayFitting", "ConduitFitting"):
    _cid = CAT.get(_key)
    if _cid is not None:
        STRATEGIES[_cid] = strategy_fitting
if CAT["Walls"] is not None:
    STRATEGIES[CAT["Walls"]] = strategy_walls
if CAT["Floors"] is not None:
    STRATEGIES[CAT["Floors"]] = strategy_floors
if CAT["Roofs"] is not None:
    STRATEGIES[CAT["Roofs"]] = strategy_roofs
if CAT["Ceilings"] is not None:
    STRATEGIES[CAT["Ceilings"]] = strategy_ceilings
for _key in ("Rooms", "MEPSpaces", "Areas"):
    _cid = CAT.get(_key)
    if _cid is not None:
        STRATEGIES[_cid] = strategy_spatial
if CAT["StructuralColumns"] is not None:
    STRATEGIES[CAT["StructuralColumns"]] = strategy_structural_column
if CAT["StructuralFraming"] is not None:
    STRATEGIES[CAT["StructuralFraming"]] = strategy_structural_framing
if CAT["CurtainWallPanels"] is not None:
    STRATEGIES[CAT["CurtainWallPanels"]] = strategy_curtain_panel
if CAT["CurtainWallMullions"] is not None:
    STRATEGIES[CAT["CurtainWallMullions"]] = strategy_curtain_mullion

# Оси, которыми владеет категорийная стратегия: generic/bbox фолбэк для них ЗАПРЕЩЁН.
STRATEGY_OWNED_AXES = {}
for _key, _axes in [
    ("Doors", set(["x", "y", "z"])),
    ("Windows", set(["x", "y", "z"])),
    ("DuctCurves", set(["x", "z"])),
    ("FlexDuctCurves", set(["x", "z"])),
    ("PipeCurves", set(["x", "z"])),
    ("FlexPipeCurves", set(["x", "z"])),
    ("CableTray", set(["x", "z"])),
    ("Conduit", set(["x", "z"])),
    ("DuctFitting", set(["x", "y", "z"])),
    ("PipeFitting", set(["x", "y", "z"])),
    ("CableTrayFitting", set(["x", "y", "z"])),
    ("ConduitFitting", set(["x", "y", "z"])),
    ("Walls", set(["y", "z"])),
    ("Floors", set(["z"])),
    ("Roofs", set(["z"])),
    ("Ceilings", set(["z"])),
    ("Rooms", set(["s", "v", "l"])),
    ("MEPSpaces", set(["s", "v", "l"])),
    ("Areas", set(["s", "v", "l"])),
    ("StructuralColumns", set(["x", "y", "z"])),
    ("StructuralFraming", set(["x", "z", "l"])),
    ("CurtainWallPanels", set(["x", "y", "z"])),
    ("CurtainWallMullions", set(["x", "y", "l"])),
]:
    _cid = CAT.get(_key)
    if _cid is not None:
        STRATEGY_OWNED_AXES[_cid] = _axes

# Оси, которые физически не имеют смысла — не пишутся вообще.
UNSUPPORTED_AXES = {}
if CAT["Walls"] is not None:
    UNSUPPORTED_AXES[CAT["Walls"]] = set(["x"])
for _key in ("DuctCurves", "FlexDuctCurves", "PipeCurves", "FlexPipeCurves", "CableTray", "Conduit"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["y"])
if CAT["StructuralFraming"] is not None:
    # У балки/каркаса осмысленны сечение (x/z) и длина (l); третья поперечная ось y — Н/П.
    UNSUPPORTED_AXES[CAT["StructuralFraming"]] = set(["y"])
for _key in ("Floors", "Roofs", "Ceilings"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["x", "y", "l"])
for _key in ("Rooms", "MEPSpaces", "Areas"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["x", "y", "z"])

# Категории, у которых l нельзя брать как max(bbox) — только curve/built-in.
LENGTH_CURVE_ONLY = set()
for _key in (
    "DuctCurves", "FlexDuctCurves", "PipeCurves", "FlexPipeCurves",
    "CableTray", "Conduit", "Walls", "StructuralFraming", "CurtainWallMullions",
):
    _cid = CAT.get(_key)
    if _cid is not None:
        LENGTH_CURVE_ONLY.add(_cid)

UNSUPPORTED_CATEGORIES = {}
for _key, _label in [
    ("Stairs", u"Категория Stairs не поддерживается: составная геометрия марша, BoundingBox не отражает реальные размеры ступени/марша."),
    ("StairsRailing", u"Категория Railings не поддерживается: составная сегментированная геометрия."),
    ("Ramps", u"Категория Ramps не поддерживается: составная геометрия пандуса."),
    ("CurtainSystem", u"Категория Curtain Systems не поддерживается: геометрия складывается из панелей/импостов, единого размера у системы нет."),
]:
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_CATEGORIES[_cid] = _label

QA_CAUTION_CATEGORIES = set()
for _key in ("DuctFitting", "PipeFitting", "CableTrayFitting", "ConduitFitting"):
    _cid = CAT.get(_key)
    if _cid is not None:
        QA_CAUTION_CATEGORIES.add(_cid)

# ---------------------------------------------------------
# 7. Запись параметров
# ---------------------------------------------------------

def set_double_param(el, name, value_internal):
    if not is_positive_number(value_internal):
        return "ЗНАЧЕНИЕ <= 0, ПРОПУЩЕНО"
    p = el.LookupParameter(name)
    if p is None:
        return "ЦЕЛЕВОЙ ПАРАМЕТР НЕ НАЙДЕН"
    if p.IsReadOnly:
        return "ЦЕЛЕВОЙ ПАРАМЕТР ТОЛЬКО ДЛЯ ЧТЕНИЯ"
    if p.StorageType != StorageType.Double:
        return "ПАРАМЕТР НЕ ЧИСЛОВОЙ (StorageType != Double)"
    p.Set(value_internal)
    return None

def clear_double_param(el, name):
    """Стирает числовой параметр (ставит 0) — для осей Н/П, чтобы убрать старые значения."""
    p = el.LookupParameter(name)
    if p is None:
        return "ЦЕЛЕВОЙ ПАРАМЕТР НЕ НАЙДЕН"
    if p.IsReadOnly:
        return "ЦЕЛЕВОЙ ПАРАМЕТР ТОЛЬКО ДЛЯ ЧТЕНИЯ"
    if p.StorageType != StorageType.Double:
        return "ПАРАМЕТР НЕ ЧИСЛОВОЙ (StorageType != Double)"
    try:
        p.Set(0.0)
    except Exception as e:
        return str(e)
    return None

COPY_TO_PIM = as_bool(IN[2], False) if len(IN) > 2 else False
INCLUDE_UNITS = as_bool(IN[3], False) if len(IN) > 3 else False

PIM_TEXT_MAP = {
    "x": u"ПИМ_Размер_Ширина",
    "y": u"ПИМ_Размер_Глубина",
    "z": u"ПИМ_Размер_Высота",
    "l": u"ПИМ_Размер_Длина",
    "s": u"ПИМ_Размер_Площадь",
    "v": u"ПИМ_Размер_Объем",
}

def _resolve_unit_config():
    """UnitTypeId (Revit 2021+) с фолбэком на DisplayUnitType для совместимости."""
    config = {}
    try:
        config = {
            "x": (UnitTypeId.Millimeters, 1, u"мм"),
            "y": (UnitTypeId.Millimeters, 1, u"мм"),
            "z": (UnitTypeId.Millimeters, 1, u"мм"),
            "l": (UnitTypeId.Millimeters, 1, u"мм"),
            "s": (UnitTypeId.SquareMeters, 0.01, u"м²"),
            "v": (UnitTypeId.CubicMeters, 0.01, u"м³"),
        }
        # smoke-check
        UnitUtils.ConvertFromInternalUnits(1.0, UnitTypeId.Millimeters)
        return config, "UnitTypeId"
    except:
        pass
    try:
        config = {
            "x": (DisplayUnitType.DUT_MILLIMETERS, 1, u"мм"),
            "y": (DisplayUnitType.DUT_MILLIMETERS, 1, u"мм"),
            "z": (DisplayUnitType.DUT_MILLIMETERS, 1, u"мм"),
            "l": (DisplayUnitType.DUT_MILLIMETERS, 1, u"мм"),
            "s": (DisplayUnitType.DUT_SQUARE_METERS, 0.01, u"м²"),
            "v": (DisplayUnitType.DUT_CUBIC_METERS, 0.01, u"м³"),
        }
        return config, "DisplayUnitType"
    except:
        return None, None

PIM_UNIT_CONFIG, PIM_UNIT_MODE = _resolve_unit_config()

def get_pim_param_name(axis, cat_id):
    if axis == "y" and cat_id in OPENING_CAT_IDS:
        return u"ПИМ_Размер_Толщина"
    return PIM_TEXT_MAP[axis]

def format_value_as_text(axis, value_internal, include_units):
    if PIM_UNIT_CONFIG is None:
        return str(value_internal)
    unit_spec, round_step, suffix = PIM_UNIT_CONFIG[axis]
    converted = UnitUtils.ConvertFromInternalUnits(value_internal, unit_spec)
    rounded = round(converted / round_step) * round_step
    if round_step >= 1:
        if rounded == int(rounded):
            rounded = int(rounded)
        text = str(rounded)
    else:
        decimals = len(str(round_step).split(".")[1])
        text = (u"{0:." + str(decimals) + u"f}").format(rounded)
    if include_units:
        text = u"{0} {1}".format(text, suffix)
    return text

def set_text_param(el, name, value_text):
    p = el.LookupParameter(name)
    if p is None:
        return "ЦЕЛЕВОЙ ПАРАМЕТР НЕ НАЙДЕН"
    if p.IsReadOnly:
        return "ЦЕЛЕВОЙ ПАРАМЕТР ТОЛЬКО ДЛЯ ЧТЕНИЯ"
    if p.StorageType != StorageType.String:
        return "ПАРАМЕТР НЕ ТЕКСТОВЫЙ (StorageType != String)"
    p.Set(value_text if value_text is not None else "")
    return None

BBOX_FLAG_PARAM = u"bbox"

def set_yesno_param(el, name, value_bool):
    p = el.LookupParameter(name)
    if p is None:
        return "ЦЕЛЕВОЙ ПАРАМЕТР НЕ НАЙДЕН"
    if p.IsReadOnly:
        return "ЦЕЛЕВОЙ ПАРАМЕТР ТОЛЬКО ДЛЯ ЧТЕНИЯ"
    if p.StorageType != StorageType.Integer:
        return "ПАРАМЕТР НЕ ТИПА ДА/НЕТ (StorageType != Integer)"
    p.Set(1 if value_bool else 0)
    return None

def axis_blocked_by_strategy(axis, owned_axes, has_strategy):
    return has_strategy and axis in owned_axes

# ---------------------------------------------------------
# 8. Основной цикл
# ---------------------------------------------------------

results = []
errors = []
sources_log = []
skipped_unsupported = []

TransactionManager.Instance.EnsureInTransaction(doc)
try:
    for el in elements:
        row = {"Id": el.Id.IntegerValue}
        src_row = {"Id": el.Id.IntegerValue}
        try:
            cat_id = get_cat_id(el)

            if cat_id in UNSUPPORTED_CATEGORIES:
                row["note"] = UNSUPPORTED_CATEGORIES[cat_id]
                skipped_unsupported.append(row)
                continue

            type_id = el.GetTypeId()
            type_elem = doc.GetElement(type_id) if type_id and type_id != ElementId.InvalidElementId else None

            # Curtain walls — составная система, не обычная стена
            if cat_id == CAT.get("Walls") and is_curtain_wall(el, type_elem):
                row["note"] = u"Curtain Wall не поддерживается как стена: обрабатывайте панели/импосты отдельно."
                skipped_unsupported.append(row)
                continue

            values = {}
            sources = {}
            unsupported_axes = UNSUPPORTED_AXES.get(cat_id, set())
            owned_axes = STRATEGY_OWNED_AXES.get(cat_id, set())
            strategy_fn = STRATEGIES.get(cat_id)
            has_strategy = strategy_fn is not None

            # --- шаг 0: категорийная стратегия ---
            if has_strategy:
                for axis, found in strategy_fn(el, type_elem).items():
                    if axis in unsupported_axes:
                        continue
                    value, src_name = found
                    if not is_positive_number(value):
                        continue
                    values[axis] = value
                    sources[axis] = u"категория: {0}".format(src_name)

            # --- шаг 1: общий built-in фолбэк ---
            for axis in ALL_AXES:
                if axis in values or axis in unsupported_axes:
                    continue
                if axis_blocked_by_strategy(axis, owned_axes, has_strategy):
                    sources[axis] = u"НЕТ ДАННЫХ (ось категории без фолбэка)"
                    continue
                found = find_existing_geometry_value(el, type_elem, axis)
                if found is not None:
                    value, src_param_name = found
                    values[axis] = value
                    sources[axis] = u"built-in: {0}".format(src_param_name)

            # --- шаг 2а: bbox / curve для незакреплённых осей ---
            missing_linear = [
                a for a in LINEAR_AXES
                if a not in values
                and a not in unsupported_axes
                and not axis_blocked_by_strategy(a, owned_axes, has_strategy)
            ]
            missing_length_axis = (
                LENGTH_AXIS not in values
                and LENGTH_AXIS not in unsupported_axes
                and not axis_blocked_by_strategy(LENGTH_AXIS, owned_axes, has_strategy)
            )

            bbox_dims = None
            if missing_linear or (missing_length_axis and cat_id not in LENGTH_CURVE_ONLY):
                bbox_dims = get_local_bbox_dimensions(el)

            if missing_linear:
                if bbox_dims is not None:
                    for axis in missing_linear:
                        if is_positive_number(bbox_dims.get(axis)):
                            values[axis] = bbox_dims[axis]
                            sources[axis] = "bbox"
                        else:
                            sources[axis] = "НЕТ ДАННЫХ (bbox <= 0)"
                else:
                    for axis in missing_linear:
                        sources[axis] = "НЕТ ДАННЫХ (ни built-in, ни bbox)"

            if missing_length_axis:
                curve_len = get_curve_length(el)
                if curve_len is not None:
                    values[LENGTH_AXIS] = curve_len
                    sources[LENGTH_AXIS] = "curve"
                elif cat_id in LENGTH_CURVE_ONLY:
                    sources[LENGTH_AXIS] = "НЕТ ДАННЫХ (только curve/built-in длины, bbox запрещён)"
                elif bbox_dims is not None:
                    max_dim = max(bbox_dims.values())
                    if is_positive_number(max_dim):
                        values[LENGTH_AXIS] = max_dim
                        sources[LENGTH_AXIS] = "bbox (max)"
                    else:
                        sources[LENGTH_AXIS] = "НЕТ ДАННЫХ (bbox <= 0)"
                else:
                    sources[LENGTH_AXIS] = "НЕТ ДАННЫХ (ни built-in, ни curve, ни bbox)"

            # --- санация линейных элементов: x/y/z не должны копировать длину ---
            length_for_check = values.get(LENGTH_AXIS)
            if length_for_check is None:
                length_for_check = get_curve_length(el)
            if length_for_check is not None:
                polluted = [
                    a for a in LINEAR_AXES
                    if a in values and nearly_equal_len(values[a], length_for_check)
                ]
                is_framing = (cat_id == CAT.get("StructuralFraming"))
                bad_section = False
                if is_framing:
                    xv = values.get("x")
                    zv = values.get("z")
                    if xv is None or zv is None:
                        bad_section = True
                    elif not is_plausible_beam_section(xv, length_for_check, trusted=False, peer=zv):
                        bad_section = True
                    elif not is_plausible_beam_section(zv, length_for_check, trusted=False, peer=xv):
                        bad_section = True
                if polluted or bad_section:
                    if is_framing:
                        tw, th = resolve_framing_section(el, type_elem, length_for_check)
                        if tw is not None and ("x" not in values or "x" in polluted or bad_section):
                            values["x"] = tw[0]
                            sources["x"] = u"санация: {0}".format(tw[1])
                        if th is not None and ("z" not in values or "z" in polluted or bad_section):
                            values["z"] = th[0]
                            sources["z"] = u"санация: {0}".format(th[1])
                    else:
                        bw, bh, bsrc = framing_section_perp_to_axis(el, length_for_check)
                        if bw is None and bh is None:
                            bw, bh, bsrc = framing_section_from_bbox(el, length_for_check)
                        if bw is not None and ("x" not in values or "x" in polluted):
                            values["x"] = bw
                            sources["x"] = u"санация: {0}".format(bsrc)
                        if bh is not None and ("z" not in values or "z" in polluted):
                            values["z"] = bh
                            sources["z"] = u"санация: {0}".format(bsrc)
                    if "y" in polluted:
                        values.pop("y", None)
                        sources["y"] = u"НЕТ ДАННЫХ (совпадало с длиной, сброшено)"
                    for axis in ("x", "z"):
                        if axis in values and nearly_equal_len(values[axis], length_for_check):
                            values.pop(axis, None)
                            sources[axis] = u"НЕТ ДАННЫХ (совпадало с длиной)"

            # --- шаг 2б: v/s по геометрии ---
            missing_scalar = [
                a for a in SCALAR_AXES
                if a not in values
                and a not in unsupported_axes
                and not axis_blocked_by_strategy(a, owned_axes, has_strategy)
            ]
            if missing_scalar:
                geom_volume, geom_area = get_geometry_volume_area(el)
                geom_values = {"v": geom_volume, "s": geom_area}
                for axis in missing_scalar:
                    gv = geom_values[axis]
                    if is_positive_number(gv):
                        values[axis] = gv
                        sources[axis] = "geometry"
                    else:
                        sources[axis] = "НЕТ ДАННЫХ (ни built-in, ни геометрия)"

            # y для категорий Н/П никогда не пишем как размер
            if "y" in unsupported_axes:
                values.pop("y", None)
                sources["y"] = u"очищено (не применимо для категории)"

            # --- запись: сначала полезные оси, потом очистка Н/П ---
            changed_any = False
            used_fallback = False
            write_errors = []

            for axis in ALL_AXES:
                if axis in unsupported_axes:
                    continue
                src = sources.get(axis, "—")
                src_row[axis] = src
                if source_is_fallback(src):
                    used_fallback = True
                if axis not in values:
                    row[axis] = "НЕТ ДАННЫХ"
                    continue
                try:
                    status = set_double_param(el, axis, values[axis])
                except Exception as write_ex:
                    status = str(write_ex)
                if status is not None:
                    row[axis] = status
                    write_errors.append(u"{0}: {1}".format(axis, status))
                    continue
                row[axis] = values[axis]
                changed_any = True

            # Очистка осей Н/П (y для балок и т.п.) — после записи, ошибка очистки не откатывает размеры
            for axis in ALL_AXES:
                if axis not in unsupported_axes:
                    continue
                try:
                    clear_status = clear_double_param(el, axis)
                except Exception as clear_ex:
                    clear_status = str(clear_ex)
                if clear_status is None:
                    changed_any = True
                if axis == "y":
                    row[axis] = u"" if clear_status is None else clear_status
                    src_row[axis] = u"очищено (не применимо для категории)"
                else:
                    row[axis] = "Н/П" if clear_status is None else clear_status
                    src_row[axis] = "Н/П (не применимо для категории)"
                if clear_status is not None:
                    write_errors.append(u"clear {0}: {1}".format(axis, clear_status))

            try:
                flag_status = set_yesno_param(el, BBOX_FLAG_PARAM, used_fallback)
            except Exception as flag_ex:
                flag_status = str(flag_ex)
            if flag_status is not None:
                row[BBOX_FLAG_PARAM] = flag_status
            else:
                row[BBOX_FLAG_PARAM] = "yes" if used_fallback else "no"
                changed_any = True

            if COPY_TO_PIM:
                for axis in PIM_TEXT_MAP:
                    pim_name = get_pim_param_name(axis, cat_id)
                    if axis in unsupported_axes:
                        text_clear = u"" if axis == "y" else u"Н/П"
                        try:
                            status = set_text_param(el, pim_name, text_clear)
                        except Exception as pim_ex:
                            status = str(pim_ex)
                        row[pim_name] = status if status is not None else text_clear
                        if status is None:
                            changed_any = True
                        continue
                    if axis not in values:
                        row[pim_name] = "НЕТ ДАННЫХ"
                        continue
                    text_value = format_value_as_text(axis, values[axis], INCLUDE_UNITS)
                    try:
                        status = set_text_param(el, pim_name, text_value)
                    except Exception as pim_ex:
                        status = str(pim_ex)
                    row[pim_name] = status if status is not None else text_value
                    if status is None:
                        changed_any = True

                if cat_id in OPENING_CAT_IDS:
                    legacy_name = PIM_TEXT_MAP["y"]
                    try:
                        clear_status = set_text_param(el, legacy_name, u"")
                    except Exception as pim_ex:
                        clear_status = str(pim_ex)
                    row[legacy_name] = clear_status if clear_status is not None else u"очищено (не применимо для проёмов)"

            sources_log.append(src_row)

            if write_errors:
                row["write_warnings"] = write_errors

            if changed_any:
                if cat_id in QA_CAUTION_CATEGORIES:
                    row["note"] = u"ФИТИНГ: размеры из коннекторов/локального bbox; проверьте ориентацию при необходимости"
                results.append(row)
            else:
                row["note"] = u"Ни один параметр не записан"
                if write_errors:
                    row["note"] = u"Ни один параметр не записан: " + u"; ".join(write_errors)
                errors.append(row)

        except Exception as e:
            row["error"] = str(e)
            errors.append(row)
finally:
    try:
        doc.Regenerate()
    except:
        pass
    TransactionManager.Instance.TransactionTaskDone()

# ---------------------------------------------------------
# 9. Итог
# ---------------------------------------------------------

summary = {
    "elements_total": len(elements),
    "processed_ok": len(results),
    "errors_or_no_change": len(errors),
    "skipped_by_workset": len(skipped_by_workset),
    "skipped_unsupported_category": len(skipped_unsupported),
    "pim_unit_mode": PIM_UNIT_MODE,
    "copy_to_pim": COPY_TO_PIM,
    "first_errors": [e for e in errors[:5]],
}

OUT = (summary, results, errors, sources_log, skipped_by_workset, skipped_unsupported)
