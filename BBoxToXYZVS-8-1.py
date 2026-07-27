# Dynamo Python Script — Revit 2023 / IronPython 2.7
#
# Архитектура: диспетчер стратегий по категории (BuiltInCategory -> функция расчёта),
# вместо единой таблицы built-in кандидатов для всех категорий сразу.
#
# ПОРЯДОК РАСЧЁТА ДЛЯ КАЖДОГО ЭЛЕМЕНТА:
#   Шаг 0. Категорийная стратегия (если для категории элемента есть спец-обработка) —
#          использует built-in параметры/API-свойства, специфичные для этой категории.
#   Шаг 1. Общий built-in built-in-фолбэк (BIP_CANDIDATES) — для осей, которые
#          не заполнила категорийная стратегия и не помечены как "не применимо".
#   Шаг 2. BoundingBox / LocationCurve / реальная геометрия — для линейных осей x/y/z/l
#          и для скаляров v/s, если built-in не дал значения.
#
# КАТЕГОРИЙНЫЕ СТРАТЕГИИ:
#
#   Doors, Windows (проёмы):
#     x = built-in "Ширина" (FAMILY_WIDTH_PARAM), z = built-in "Высота" (FAMILY_HEIGHT_PARAM),
#     y = параметр семейства "Толщина" (LookupParameter, экземпляр -> тип; для окон
#     дополнительно перебираются синонимы — см. WINDOW_THICKNESS_PARAM_NAMES).
#     Общий built-in/bbox поиск для этих трёх осей не выполняется — это была основная
#     причина ошибки "y = ширина вместо толщины", которую чинили ранее.
#
#   DuctCurves, PipeCurves, CableTray, Conduit (прямые MEP-участки):
#     Круглое сечение: x = z = диаметр (RBS_CURVE_DIAMETER_PARAM / RBS_PIPE_DIAMETER_PARAM /
#     RBS_CONDUIT_DIAMETER_PARAM). Прямоугольное сечение: x = ширина, z = высота
#     (RBS_CURVE_WIDTH/HEIGHT_PARAM, RBS_CABLETRAY_WIDTH/HEIGHT_PARAM).
#     y для этих категорий помечена "не применимо" — у линейного MEP-элемента только
#     2 осмысленных поперечных размера (или 1 для круглого), третьего "y" физически нет.
#
#   DuctFitting/PipeFitting/CableTrayFitting/ConduitFitting (фитинги):
#     НЕ обрабатываются спец-стратегией. У фитингов Revit может произвольно менять местами
#     Width/Height built-in параметры в зависимости от ориентации/поворота фитинга
#     (подтверждённый баг Revit, а не ошибка скрипта) — поэтому для них используется общий
#     built-in/bbox путь, а в отчёте на такие элементы ставится пометка "ТРЕБУЕТ ПРОВЕРКИ".
#
#   Walls:
#     z = высота (WALL_USER_HEIGHT_PARAM), y = толщина (WALL_ATTR_WIDTH_PARAM, параметр ТИПА).
#     x помечена "не применимо" — у стены нет осмысленного мирового "x": bbox по мировой
#     оси X зависит от угла поворота стены в плане и не является её размером.
#     l (длина) считается как обычно — через LocationCurve/built-in, без изменений.
#
#   Floors, Roofs, Ceilings (плоскостные хосты):
#     z = суммарная толщина типа через CompoundStructure.GetWidth() (если у типа есть
#     многослойная структура). x, y, l помечены "не применимо" — bbox описанного
#     прямоугольника вокруг произвольной плиты/крыши не является её реальным размером.
#     s/v считаются как обычно (HOST_AREA_COMPUTED / HOST_VOLUME_COMPUTED -> геометрия).
#
#   Rooms, MEPSpaces, Areas (помещения/зоны):
#     s = SpatialElement.Area, v = SpatialElement.Volume, l = SpatialElement.Perimeter
#     (родные свойства API, не параметры — надёжнее built-in параметров и не зависят
#     от версии Revit). x, y, z помечены "не применимо" — помещение обычно непрямоугольное,
#     bbox не отражает его размеры.
#
#   Stairs, StairsRailing (Railings), Ramps, CurtainSystem:
#     категории целиком ИСКЛЮЧЕНЫ из расчёта (составная/сегментированная геометрия,
#     bbox по всему маршу/пролёту не является размером элемента). Ни один параметр
#     не пишется, элемент попадает в отдельный список "skipped_unsupported" с пояснением.
#
#   Всё остальное (Furniture, Casework, Generic Models, Structural Columns/Framing,
#   Mechanical/Electrical/Plumbing Equipment и т.д.):
#     работает как раньше — общий built-in-фолбэк (BIP_CANDIDATES) -> BoundingBox.
#     BoundingBox для family instance считается в ЛОКАЛЬНОЙ системе координат типа
#     (см. get_local_bbox_dimensions) — корректно для повёрнутых/наклонных экземпляров
#     любой категории, а не только структурных колонн.
#
# Для l (продольная длина — балки, трубы, воздуховоды, стены и т.п.):
#   1. Built-in параметр (CURVE_ELEM_LENGTH / INSTANCE_LENGTH_PARAM / STRUCTURAL_FRAME_CUT_LENGTH).
#   2. Фолбэк — длина LocationCurve элемента (по кривой оси; для дуг — длина дуги, не хорда).
#   3. Фолбэк — максимальный из трёх габаритов BoundingBox (последняя попытка).
#
# Для v (объём), s (площадь) — там, где нет категорийной стратегии:
#   1. Built-in параметр (HOST_VOLUME_COMPUTED / HOST_AREA_COMPUTED), сначала экземпляр, потом тип.
#   2. Фолбэк — реальная геометрия элемента: сумма Solid.Volume и сумма площадей всех граней.
#
# Все значения пишутся как есть, во внутренних единицах Revit, в параметры проекта
# x / y / z / l (тип "Длина"), v (тип "Объём"), s (тип "Площадь"). Оси, помеченные
# "не применимо" для категории элемента, не записываются вообще (чтобы не оставлять
# в модели вычисленный, но бессмысленный 0 или мусорное значение).

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')
from Autodesk.Revit.DB import *
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument

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
#     Принимает имя набора (строка) или список имён. Если IN[1] не задан/пустой — фильтр не применяется.
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
# 2. Базовые хелперы: безопасный доступ к BuiltInParameter / BuiltInCategory
#    (getattr с default=None — на случай, если константа отсутствует в этой версии API)
# ---------------------------------------------------------

def bip(name):
    return getattr(BuiltInParameter, name, None)

def catid(name):
    bc = getattr(BuiltInCategory, name, None)
    return int(bc) if bc is not None else None

def get_builtin_value(elem_or_type, bip_enum):
    """Возвращает (значение, имя_параметра), если параметр найден, заполнен и числовой."""
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
        name = p.Definition.Name
    except:
        name = str(bip_enum)
    return (p.AsDouble(), name)

def get_val_inst_or_type(el, type_elem, bip_enum):
    """get_builtin_value сначала на экземпляре, потом (если пусто) на типе."""
    found = get_builtin_value(el, bip_enum)
    if found is None and type_elem is not None:
        found = get_builtin_value(type_elem, bip_enum)
    return found

def get_cat_id(el):
    try:
        return el.Category.Id.IntegerValue if el.Category is not None else None
    except:
        return None

# ---------------------------------------------------------
# 3. Общая таблица built-in кандидатов (универсальный фолбэк для категорий
#    без собственной стратегии — мебель, оборудование, generic models и т.п.)
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
    """Общий built-in-поиск: перебирает кандидатов из BIP_CANDIDATES[axis]."""
    for bip_enum in BIP_CANDIDATES[axis]:
        found = get_val_inst_or_type(el, type_elem, bip_enum)
        if found is not None:
            return found
    return None

# ---------------------------------------------------------
# 4. Категории со спец-обработкой
# ---------------------------------------------------------

CAT = {
    "Doors": catid("OST_Doors"),
    "Windows": catid("OST_Windows"),
    "DuctCurves": catid("OST_DuctCurves"),
    "DuctFitting": catid("OST_DuctFitting"),
    "PipeCurves": catid("OST_PipeCurves"),
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
}

# --- 4a. Doors / Windows (проёмы) ---

DOOR_THICKNESS_PARAM_NAMES = [u"Толщина"]
WINDOW_THICKNESS_PARAM_NAMES = [u"Толщина", u"Толщина коробки", u"Толщина рамы"]

def make_opening_strategy(thickness_names):
    def strategy(el, type_elem):
        result = {}
        w = get_val_inst_or_type(el, type_elem, bip("FAMILY_WIDTH_PARAM"))
        if w is not None:
            result["x"] = w
        h = get_val_inst_or_type(el, type_elem, bip("FAMILY_HEIGHT_PARAM"))
        if h is not None:
            result["z"] = h
        for pname in thickness_names:
            p = el.LookupParameter(pname)
            if (p is None or not p.HasValue) and type_elem is not None:
                p = type_elem.LookupParameter(pname)
            if p is not None and p.HasValue and p.StorageType == StorageType.Double:
                result["y"] = (p.AsDouble(), u"семейство: {0}".format(pname))
                break
        return result
    return strategy

strategy_doors = make_opening_strategy(DOOR_THICKNESS_PARAM_NAMES)
strategy_windows = make_opening_strategy(WINDOW_THICKNESS_PARAM_NAMES)

OPENING_CAT_IDS = set([c for c in (CAT["Doors"], CAT["Windows"]) if c is not None])

# --- 4b. Прямые MEP-участки (Duct/Pipe/CableTray/Conduit Curves) ---

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

# --- 4c. Walls ---

def strategy_walls(el, type_elem):
    result = {}
    h = get_val_inst_or_type(el, type_elem, bip("WALL_USER_HEIGHT_PARAM"))
    if h is not None:
        result["z"] = h
    w = get_val_inst_or_type(el, type_elem, bip("WALL_ATTR_WIDTH_PARAM"))
    if w is not None:
        result["y"] = w
    return result

# --- 4d. Floors / Roofs / Ceilings (плоскостные хосты) ---

def strategy_planar_host(el, type_elem):
    result = {}
    if type_elem is not None:
        try:
            cs = type_elem.GetCompoundStructure()
        except:
            cs = None
        if cs is not None:
            try:
                thickness = cs.GetWidth()
                if thickness and thickness > 0:
                    result["z"] = (thickness, u"CompoundStructure (общая толщина типа)")
            except:
                pass
    return result

# --- 4e. Rooms / MEPSpaces / Areas (помещения/зоны) ---

def strategy_spatial(el, type_elem):
    result = {}
    try:
        area = el.Area
        if area and area > 0:
            result["s"] = (area, u"SpatialElement.Area")
    except:
        pass
    try:
        vol = el.Volume
        if vol and vol > 0:
            result["v"] = (vol, u"SpatialElement.Volume")
    except:
        pass
    try:
        perim = el.Perimeter
        if perim and perim > 0:
            result["l"] = (perim, u"SpatialElement.Perimeter")
    except:
        pass
    return result

# ---------------------------------------------------------
# 5. Таблицы диспетчеризации: категория -> стратегия / запрещённые оси / полностью вне поддержки
# ---------------------------------------------------------

STRATEGIES = {}
if CAT["Doors"] is not None:
    STRATEGIES[CAT["Doors"]] = strategy_doors
if CAT["Windows"] is not None:
    STRATEGIES[CAT["Windows"]] = strategy_windows
if CAT["DuctCurves"] is not None:
    STRATEGIES[CAT["DuctCurves"]] = strategy_duct
if CAT["PipeCurves"] is not None:
    STRATEGIES[CAT["PipeCurves"]] = strategy_pipe
if CAT["CableTray"] is not None:
    STRATEGIES[CAT["CableTray"]] = strategy_cabletray
if CAT["Conduit"] is not None:
    STRATEGIES[CAT["Conduit"]] = strategy_conduit
if CAT["Walls"] is not None:
    STRATEGIES[CAT["Walls"]] = strategy_walls
for _key in ("Floors", "Roofs", "Ceilings"):
    _cid = CAT.get(_key)
    if _cid is not None:
        STRATEGIES[_cid] = strategy_planar_host
for _key in ("Rooms", "MEPSpaces", "Areas"):
    _cid = CAT.get(_key)
    if _cid is not None:
        STRATEGIES[_cid] = strategy_spatial

# Оси, которые для данной категории физически не имеют смысла — не пишутся вообще,
# даже если бы generic built-in/bbox фолбэк что-то на них "нашёл".
UNSUPPORTED_AXES = {}
if CAT["Walls"] is not None:
    UNSUPPORTED_AXES[CAT["Walls"]] = set(["x"])
for _key in ("DuctCurves", "PipeCurves", "CableTray", "Conduit"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["y"])
for _key in ("Floors", "Roofs", "Ceilings"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["x", "y", "l"])
for _key in ("Rooms", "MEPSpaces", "Areas"):
    _cid = CAT.get(_key)
    if _cid is not None:
        UNSUPPORTED_AXES[_cid] = set(["x", "y", "z"])

# Категории, полностью исключённые из расчёта (составная/сегментированная геометрия) —
# по ним не пишется ни один параметр, элемент попадает в skipped_unsupported.
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

# Категории-фитинги: известная особенность Revit — built-in Width/Height могут меняться
# местами в зависимости от ориентации фитинга. Спец-стратегии для них нет (идут по общему
# built-in/bbox пути), но элемент помечается в отчёте как требующий ручной проверки.
QA_CAUTION_CATEGORIES = set()
for _key in ("DuctFitting", "PipeFitting", "CableTrayFitting", "ConduitFitting"):
    _cid = CAT.get(_key)
    if _cid is not None:
        QA_CAUTION_CATEGORIES.add(_cid)

# ---------------------------------------------------------
# 6. Геометрические фолбэки (BoundingBox / LocationCurve / реальная геометрия)
# ---------------------------------------------------------

def get_bbox_dimensions(el):
    """Мировой (axis-aligned) bbox — используется, если локальный недоступен."""
    bb = el.get_BoundingBox(None)
    if bb is None:
        return None
    dx = abs(bb.Max.X - bb.Min.X)
    dy = abs(bb.Max.Y - bb.Min.Y)
    dz = abs(bb.Max.Z - bb.Min.Z)
    return {"x": dx, "y": dy, "z": dz}

_GEOM_OPTIONS = Options()
_GEOM_OPTIONS.ComputeReferences = False
_GEOM_OPTIONS.IncludeNonVisibleObjects = False
try:
    _GEOM_OPTIONS.DetailLevel = ViewDetailLevel.Fine
except:
    pass

def get_local_bbox_dimensions(el):
    """Bbox в ЛОКАЛЬНОЙ системе координат типа (GetSymbolGeometry — геометрия ДО применения
    поворота/наклона экземпляра). Даёт корректные габариты сечения независимо от поворота
    элемента в проекте — работает для ЛЮБОЙ повёрнутой/наклонённой family instance (мебель,
    оборудование, генерик-модели), не только для структурных колонн. Если у элемента нет
    GeometryInstance (стены, полы, in-place) — откатывается на обычный мировой bbox."""
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
    """Длина элемента по его LocationCurve (балки/трубы/колонны/стены и т.п.),
    либо None, если у элемента нет LocationCurve. Для дуг — длина дуги, не хорда."""
    try:
        loc = el.Location
    except:
        return None
    if isinstance(loc, LocationCurve):
        try:
            length = loc.Curve.Length
            if length and length > 0:
                return length
        except:
            return None
    return None

def get_geometry_volume_area(el):
    """Считает объём/площадь по реальной геометрии: сумма Solid.Volume и сумма площадей
    всех граней всех солидов (с разворачиванием GeometryInstance для family instance).
    Возвращает (volume, area) в внутренних единицах, либо (None, None)."""
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

def set_double_param(el, name, value_internal):
    p = el.LookupParameter(name)
    if p is None:
        return "ЦЕЛЕВОЙ ПАРАМЕТР НЕ НАЙДЕН"
    if p.IsReadOnly:
        return "ЦЕЛЕВОЙ ПАРАМЕТР ТОЛЬКО ДЛЯ ЧТЕНИЯ"
    if p.StorageType != StorageType.Double:
        return "ПАРАМЕТР НЕ ЧИСЛОВОЙ (StorageType != Double)"
    p.Set(value_internal)
    return None

# ---------------------------------------------------------
# 7. Опциональное копирование x/y/z/l/s/v в текстовые параметры ПИМ_Размер_*
#    Управляется через IN[2] (копировать: Да/Нет) и IN[3] (добавлять единицы измерения: Да/Нет).
# ---------------------------------------------------------

COPY_TO_PIM = bool(IN[2]) if len(IN) > 2 else False
INCLUDE_UNITS = bool(IN[3]) if len(IN) > 3 else False

PIM_TEXT_MAP = {
    "x": u"ПИМ_Размер_Ширина",
    "y": u"ПИМ_Размер_Глубина",
    "z": u"ПИМ_Размер_Высота",
    "l": u"ПИМ_Размер_Длина",
    "s": u"ПИМ_Размер_Площадь",
    "v": u"ПИМ_Размер_Объем",
}

PIM_UNIT_CONFIG = {
    "x": (UnitTypeId.Millimeters, 1, u"мм"),
    "y": (UnitTypeId.Millimeters, 1, u"мм"),
    "z": (UnitTypeId.Millimeters, 1, u"мм"),
    "l": (UnitTypeId.Millimeters, 1, u"мм"),
    "s": (UnitTypeId.SquareMeters, 0.01, u"м²"),
    "v": (UnitTypeId.CubicMeters, 0.01, u"м³"),
}

def get_pim_param_name(axis, cat_id):
    """Для дверей/окон ось y пишем в ПИМ_Размер_Толщина, а не в ПИМ_Размер_Глубина."""
    if axis == "y" and cat_id in OPENING_CAT_IDS:
        return u"ПИМ_Размер_Толщина"
    return PIM_TEXT_MAP[axis]

def format_value_as_text(axis, value_internal, include_units):
    unit_type_id, round_step, suffix = PIM_UNIT_CONFIG[axis]
    converted = UnitUtils.ConvertFromInternalUnits(value_internal, unit_type_id)
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

# ---------------------------------------------------------
# 8. Основной цикл
# ---------------------------------------------------------

results = []
errors = []
sources_log = []          # для отладки: откуда взято каждое значение (built-in / bbox / geometry / категория)
skipped_unsupported = []  # элементы категорий, полностью исключённых из расчёта

TransactionManager.Instance.EnsureInTransaction(doc)

for el in elements:
    row = {"Id": el.Id.IntegerValue}
    src_row = {"Id": el.Id.IntegerValue}
    try:
        cat_id = get_cat_id(el)

        # --- категория целиком не поддерживается (Stairs/Railings/Ramps/CurtainSystem) ---
        if cat_id in UNSUPPORTED_CATEGORIES:
            row["note"] = UNSUPPORTED_CATEGORIES[cat_id]
            skipped_unsupported.append(row)
            continue

        type_id = el.GetTypeId()
        type_elem = doc.GetElement(type_id) if type_id and type_id != ElementId.InvalidElementId else None

        values = {}
        sources = {}
        unsupported_axes = UNSUPPORTED_AXES.get(cat_id, set())

        # --- шаг 0: категорийная стратегия ---
        strategy_fn = STRATEGIES.get(cat_id)
        if strategy_fn is not None:
            for axis, found in strategy_fn(el, type_elem).items():
                if axis in unsupported_axes:
                    continue
                value, src_name = found
                values[axis] = value
                sources[axis] = u"категория: {0}".format(src_name)

        # --- шаг 1: общий built-in фолбэк (кроме уже найденных и "не применимо" осей) ---
        for axis in ALL_AXES:
            if axis in values or axis in unsupported_axes:
                continue
            found = find_existing_geometry_value(el, type_elem, axis)
            if found is not None:
                value, src_param_name = found
                values[axis] = value
                sources[axis] = u"built-in: {0}".format(src_param_name)

        # --- шаг 2а: фолбэк для x/y/z -> BoundingBox, для l -> LocationCurve -> max(bbox) ---
        missing_linear = [a for a in LINEAR_AXES if a not in values and a not in unsupported_axes]
        missing_length_axis = (LENGTH_AXIS not in values) and (LENGTH_AXIS not in unsupported_axes)

        bbox_dims = None
        if missing_linear or missing_length_axis:
            bbox_dims = get_local_bbox_dimensions(el)

        if missing_linear:
            if bbox_dims is not None:
                for axis in missing_linear:
                    values[axis] = bbox_dims[axis]
                    sources[axis] = "bbox"
            else:
                for axis in missing_linear:
                    sources[axis] = "НЕТ ДАННЫХ (ни built-in, ни bbox)"

        if missing_length_axis:
            curve_len = get_curve_length(el)
            if curve_len is not None:
                values[LENGTH_AXIS] = curve_len
                sources[LENGTH_AXIS] = "curve"
            elif bbox_dims is not None:
                values[LENGTH_AXIS] = max(bbox_dims.values())
                sources[LENGTH_AXIS] = "bbox (max)"
            else:
                sources[LENGTH_AXIS] = "НЕТ ДАННЫХ (ни built-in, ни curve, ни bbox)"

        # --- шаг 2б: фолбэк для v/s -> реальная геометрия ---
        missing_scalar = [a for a in SCALAR_AXES if a not in values and a not in unsupported_axes]
        if missing_scalar:
            geom_volume, geom_area = get_geometry_volume_area(el)
            geom_values = {"v": geom_volume, "s": geom_area}
            for axis in missing_scalar:
                gv = geom_values[axis]
                if gv is not None:
                    values[axis] = gv
                    sources[axis] = "geometry"
                else:
                    sources[axis] = "НЕТ ДАННЫХ (ни built-in, ни геометрия)"

        # --- запись в параметры x/y/z/l/s/v ---
        changed_any = False
        used_fallback = False
        for axis in ALL_AXES:
            if axis in unsupported_axes and axis not in values:
                row[axis] = "Н/П"
                src_row[axis] = "Н/П (не применимо для категории)"
                continue
            src = sources.get(axis, "—")
            src_row[axis] = src
            if src in ("bbox", "geometry", "bbox (max)"):
                used_fallback = True
            if axis not in values:
                row[axis] = "НЕТ ДАННЫХ"
                continue
            status = set_double_param(el, axis, values[axis])
            if status is not None:
                row[axis] = status
                continue
            row[axis] = values[axis]
            changed_any = True

        flag_status = set_yesno_param(el, BBOX_FLAG_PARAM, used_fallback)
        if flag_status is not None:
            row[BBOX_FLAG_PARAM] = flag_status
        else:
            row[BBOX_FLAG_PARAM] = "yes" if used_fallback else "no"

        # --- шаг 3: опциональное копирование в текстовые ПИМ_Размер_* ---
        if COPY_TO_PIM:
            for axis in PIM_TEXT_MAP:
                pim_name = get_pim_param_name(axis, cat_id)
                if axis in unsupported_axes and axis not in values:
                    status = set_text_param(el, pim_name, u"Н/П")
                    row[pim_name] = status if status is not None else u"Н/П"
                    continue
                if axis not in values:
                    row[pim_name] = "НЕТ ДАННЫХ"
                    continue
                text_value = format_value_as_text(axis, values[axis], INCLUDE_UNITS)
                status = set_text_param(el, pim_name, text_value)
                row[pim_name] = status if status is not None else text_value

            # у проёмов (Doors/Windows) ось y теперь пишется в ПИМ_Размер_Толщина,
            # а не в ПИМ_Размер_Глубина — чистим Глубину от устаревших значений
            if cat_id in OPENING_CAT_IDS:
                legacy_name = PIM_TEXT_MAP["y"]  # ПИМ_Размер_Глубина
                clear_status = set_text_param(el, legacy_name, u"")
                row[legacy_name] = clear_status if clear_status is not None else u"очищено (не применимо для проёмов)"

        sources_log.append(src_row)

        if changed_any:
            if cat_id in QA_CAUTION_CATEGORIES:
                row["note"] = u"ФИТИНГ: Revit может менять местами Ширину/Высоту в зависимости от ориентации — проверьте значение вручную"
            results.append(row)
        else:
            row["note"] = "Ни один параметр не записан"
            errors.append(row)

    except Exception as e:
        row["error"] = str(e)
        errors.append(row)

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
}

OUT = (summary, results, errors, sources_log, skipped_by_workset, skipped_unsupported)
