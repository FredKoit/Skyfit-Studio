import sys
import struct
import zlib
import json
import shutil
from copy import deepcopy
from datetime import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QComboBox, QSpinBox, QCheckBox,
    QTabWidget, QTextEdit, QSplitter, QMessageBox, QAbstractItemView, QGroupBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon

# =====================================================================
# 1. PURE-PYTHON BINARY SKYRIM PLUGIN PARSER (.ESP / .ESM / .ESL)
# =====================================================================

DEFAULT_PRESETS = ["(None / Random)"]

@dataclass
class SkyrimRecord:
    plugin_name: str
    record_type: str      # 'OTFT' or 'ARMO'
    form_id_hex: str      # e.g., "0x800" (load-order stripped for SPID/SkyPatcher)
    editor_id: str        # e.g., "DX_Succubus_Outfit"
    full_name: str = ""
    item_count: int = 0
    refit_items: tuple[tuple[str, str], ...] = ()
    refit_notes: tuple[str, ...] = ()

class SkyrimPluginParser:
    """Reads Skyrim SE/AE binary plugins and extracts local OTFT and ARMO records."""

    @staticmethod
    def parse_plugin(filepath: Path, resolver: "RefitResolver" = None) -> List[SkyrimRecord]:
        records: List[SkyrimRecord] = []
        plugin_name = filepath.name

        try:
            data = filepath.read_bytes()
        except Exception as e:
            print(f"Error reading {plugin_name}: {e}")
            return records

        if len(data) < 24 or data[0:4] != b'TES4':
            return records

        tes4_size, tes4_flags = struct.unpack('<II', data[4:12])
        is_esl = bool(tes4_flags & 0x200) or plugin_name.lower().endswith('.esl')

        # Count master files (MAST subrecords inside TES4)
        master_count = 0
        pos = 24
        tes4_end = 24 + tes4_size
        while pos + 6 <= tes4_end and pos + 6 <= len(data):
            sub_type = data[pos:pos+4]
            sub_size = struct.unpack('<H', data[pos+4:pos+6])[0]
            if sub_type == b'MAST':
                master_count += 1
            pos += 6 + sub_size

        pos = tes4_end
        data_len = len(data)

        while pos + 24 <= data_len:
            rec_type = data[pos:pos+4]
            if rec_type == b'GRUP':
                grup_size = struct.unpack('<I', data[pos+4:pos+8])[0]
                grup_label = data[pos+8:pos+12]
                if grup_size < 24:
                    break

                if grup_label in (b'OTFT', b'ARMO'):
                    SkyrimPluginParser._parse_grup(
                        data, pos + 24, pos + grup_size,
                        plugin_name, is_esl, master_count, records
                    )
                pos += grup_size
            else:
                rec_size = struct.unpack('<I', data[pos+4:pos+8])[0]
                pos += 24 + rec_size

        resolver = resolver or RefitResolver([filepath])
        for record in records:
            if record.record_type == "OTFT":
                record.refit_items, record.refit_notes = resolver.resolve(filepath, int(record.form_id_hex, 16))
        return records

    @staticmethod
    def _parse_grup(data: bytes, start: int, end: int, plugin_name: str,
                    is_esl: bool, master_count: int, out_records: List[SkyrimRecord]):
        pos = start
        while pos + 24 <= end and pos + 24 <= len(data):
            rec_type = data[pos:pos+4]
            if rec_type == b'GRUP':
                grup_size = struct.unpack('<I', data[pos+4:pos+8])[0]
                if grup_size < 24:
                    break
                pos += grup_size
                continue

            rec_size, rec_flags, form_id = struct.unpack('<III', data[pos+4:pos+16])
            rec_data_start = pos + 24
            rec_data_end = rec_data_start + rec_size
            pos = rec_data_end

            if rec_type not in (b'OTFT', b'ARMO') or rec_data_end > len(data):
                continue

            # Exclude master-file overrides
            mod_index = (form_id >> 24) & 0xFF
            if mod_index < master_count and master_count > 0:
                continue

            local_id = (form_id & 0xFFF) if is_esl else (form_id & 0xFFFFFF)
            form_id_hex = f"0x{local_id:X}"

            raw_sub_data = data[rec_data_start:rec_data_end]

            # Handle zlib compressed records (Flag 0x00040000)
            if rec_flags & 0x00040000:
                if len(raw_sub_data) > 4:
                    try:
                        raw_sub_data = zlib.decompress(raw_sub_data[4:])
                    except Exception:
                        continue
                else:
                    continue

            editor_id = ""
            full_name = ""
            item_count = 0
            s_pos = 0
            s_len = len(raw_sub_data)
            xxxx_size = None

            while s_pos + 6 <= s_len:
                sub_type = raw_sub_data[s_pos:s_pos+4]
                sub_size = struct.unpack('<H', raw_sub_data[s_pos+4:s_pos+6])[0]
                s_pos += 6

                if sub_type == b'XXXX':
                    if s_pos + 4 <= s_len:
                        xxxx_size = struct.unpack('<I', raw_sub_data[s_pos:s_pos+4])[0]
                    s_pos += sub_size
                    continue

                if xxxx_size is not None:
                    sub_size = xxxx_size
                    xxxx_size = None

                sub_payload = raw_sub_data[s_pos:s_pos+sub_size]
                s_pos += sub_size

                if sub_type == b'EDID':
                    editor_id = sub_payload.rstrip(b'\x00').decode('utf-8', errors='ignore')
                elif sub_type == b'FULL':
                    try:
                        extracted_full = sub_payload.rstrip(b'\x00').decode('utf-8', errors='ignore')
                        if extracted_full and extracted_full.isprintable():
                            full_name = extracted_full
                    except Exception:
                        pass
                elif sub_type == b'INAM' and rec_type == b'OTFT':
                    item_count += len(sub_payload) // 4

            if editor_id:
                out_records.append(SkyrimRecord(
                    plugin_name=plugin_name,
                    record_type=rec_type.decode('ascii'),
                    form_id_hex=form_id_hex,
                    editor_id=editor_id,
                    full_name=full_name,
                    item_count=item_count
                ))


class RefitResolver:
    """Resolve on-disk OTFT/LVLI references to verified ARMO FormIDs.

    Master indexes belong to the plugin containing each reference, not to the
    current load order. Missing or unsupported references never become exports.
    """

    def __init__(self, plugins=(), data_dirs=None):
        self.paths = {Path(p).name.casefold(): Path(p) for p in plugins}
        self.data_dirs = list(data_dirs) if data_dirs is not None else self.installed_data_dirs()
        self.cache = {}

    @staticmethod
    def installed_data_dirs():
        directories = []
        if sys.platform == "win32":
            import winreg
            for key in (r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition",
                        r"SOFTWARE\WOW6432Node\Bethesda Softworks\Skyrim Special Edition"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                        game_path = winreg.QueryValueEx(handle, "Installed Path")[0]
                    directory = Path(game_path) / "Data"
                    if directory.is_dir() and directory not in directories:
                        directories.append(directory)
                except OSError:
                    continue
        return directories

    @staticmethod
    def subrecords(payload):
        pos = 0
        extended = None
        while pos + 6 <= len(payload):
            tag = payload[pos:pos + 4]
            size = struct.unpack_from("<H", payload, pos + 4)[0]
            pos += 6
            if tag == b"XXXX":
                if size != 4 or pos + 4 > len(payload):
                    raise ValueError("Invalid extended subrecord")
                extended = struct.unpack_from("<I", payload, pos)[0]
                pos += 4
                continue
            if extended is not None:
                size, extended = extended, None
            if pos + size > len(payload):
                raise ValueError("Truncated subrecord")
            yield tag, payload[pos:pos + size]
            pos += size

    def _load(self, path):
        path = Path(path).resolve()
        if path in self.cache:
            return self.cache[path]
        try:
            data = path.read_bytes()
            if len(data) < 24 or data[:4] != b"TES4":
                raise ValueError("Not a Skyrim plugin")
            size, flags = struct.unpack_from("<II", data, 4)
            if size + 24 > len(data):
                raise ValueError("Truncated plugin header")
            masters = tuple(value.rstrip(b"\0").decode("utf-8")
                            for tag, value in self.subrecords(data[24:24 + size]) if tag == b"MAST")
            nodes = {}

            def visit(start, end, depth=0):
                if depth > 32:
                    raise ValueError("Too many nested record groups")
                pos = start
                while pos + 24 <= end:
                    tag = data[pos:pos + 4]
                    length = struct.unpack_from("<I", data, pos + 4)[0]
                    if tag == b"GRUP":
                        if length < 24 or pos + length > end:
                            raise ValueError("Invalid record group")
                        if depth or data[pos + 8:pos + 12] in (b"ARMO", b"OTFT", b"LVLI"):
                            visit(pos + 24, pos + length, depth + 1)
                        pos += length
                        continue
                    if pos + 24 + length > end:
                        raise ValueError("Truncated record")
                    record_flags, form_id = struct.unpack_from("<II", data, pos + 8)
                    if tag in (b"ARMO", b"OTFT", b"LVLI") and not record_flags & 0x20:
                        payload = data[pos + 24:pos + 24 + length]
                        if record_flags & 0x40000:
                            if len(payload) < 4:
                                raise ValueError("Invalid compressed record")
                            payload = zlib.decompress(payload[4:])
                        children = []
                        if tag != b"ARMO":
                            for sub_type, value in self.subrecords(payload):
                                if tag == b"OTFT" and sub_type == b"INAM":
                                    if len(value) % 4:
                                        raise ValueError("Invalid outfit item array")
                                    children.extend(struct.unpack("<" + "I" * (len(value) // 4), value))
                                elif tag == b"LVLI" and sub_type == b"LVLO":
                                    if len(value) < 12:
                                        raise ValueError("Invalid leveled-list entry")
                                    children.append(struct.unpack_from("<I", value, 4)[0])
                        nodes[form_id] = (tag, tuple(children))
                    pos += 24 + length

            visit(24 + size, len(data))
            result = (masters, bool(flags & 0x200) or path.suffix.lower() == ".esl", nodes)
        except (OSError, ValueError, UnicodeError, struct.error, zlib.error):
            result = None
        self.cache[path] = result
        return result

    def _path_for(self, name, context):
        if name.casefold() in self.paths:
            return self.paths[name.casefold()]
        for directory in (Path(context).parent, *self.data_dirs):
            candidate = directory / name
            if candidate.is_file():
                self.paths[name.casefold()] = candidate
                return candidate
        return None

    def resolve(self, filepath, local_id):
        filepath = Path(filepath)
        self.paths.setdefault(filepath.name.casefold(), filepath)
        result, notes = [], []
        index = self._load(filepath)
        if index is None:
            return (), (f"Cannot read {filepath.name} to verify Force ORefit items.",)

        def walk(context, form_id, stack):
            if form_id == 0:
                notes.append("Skipped an empty item reference.")
                return
            current = self._load(context)
            if current is None:
                notes.append(f"Cannot read {Path(context).name} for Force ORefit.")
                return
            masters, is_light, nodes = current
            owner_index = form_id >> 24
            if owner_index > len(masters):
                notes.append(f"Invalid master index in {Path(context).name}: {form_id:08X}.")
                return
            owner_name = masters[owner_index] if owner_index < len(masters) else Path(context).name
            local = form_id & 0xFFFFFF
            node = nodes.get(form_id)
            node_context = Path(context)
            owner_light = is_light if owner_index == len(masters) else False
            if owner_index < len(masters):
                owner_path = self._path_for(owner_name, context)
                owner_data = self._load(owner_path) if owner_path else None
                if owner_data is not None:
                    owner_light = owner_data[1]
                    if node is None:
                        node = owner_data[2].get((len(owner_data[0]) << 24) | local)
                        node_context = owner_path
                elif node is None:
                    notes.append(f"Missing or unreadable master {owner_name}; skipped item {local:X}.")
                    return
            key = (str(Path(node_context).resolve()).casefold(), form_id if node_context == Path(context) else local)
            if key in stack or len(stack) >= 64:
                notes.append(f"Skipped a cyclic or deeply nested item list in {owner_name}.")
                return
            if node is None:
                notes.append(f"No wearable armor or supported item list found for {owner_name}|{local:X}.")
                return
            tag, children = node
            if tag == b"ARMO":
                reference = (owner_name, f"{local & (0xFFF if owner_light else 0xFFFFFF):X}")
                if reference not in result:
                    result.append(reference)
            elif tag in (b"OTFT", b"LVLI"):
                if not children:
                    notes.append(f"Empty item list {owner_name}|{local:X}; no armor to refit.")
                for child in children:
                    walk(node_context, child, stack | {key})

        walk(filepath, (len(index[0]) << 24) | local_id, set())
        return tuple(result), tuple(dict.fromkeys(notes))


# =====================================================================
# 2. BODYSLIDE XML PRESET SCANNER (FOR OBODY NG)
# =====================================================================

class BodySlideScanner:
    """Scans CalienteTools/BodySlide/SliderPresets for XML preset names."""

    @staticmethod
    def scan_presets(folder: Path) -> List[str]:
        presets = set()
        for xml_file in folder.rglob("*.xml"):
            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()
                for preset in root.findall(".//Preset"):
                    name = preset.attrib.get("name")
                    if name:
                        presets.add(name)
            except Exception:
                continue
        return sorted(presets)

    @staticmethod
    def scan_presets_by_gender(folder: Path) -> Dict[str, List[str]]:
        found = {"Female": set(), "Male": set(), "Unknown": set()}
        for xml_file in folder.rglob("*.xml"):
            try:
                root = ET.parse(xml_file).getroot()
                for preset in root.findall(".//Preset"):
                    name = preset.attrib.get("name")
                    if not name:
                        continue
                    body = " ".join([preset.attrib.get("set", "")] +
                                    [group.attrib.get("name", "") for group in preset.findall("Group")]).lower()
                    female = any(token in body for token in ("cbbe", "3ba", "3bbb", "bhunp", "uunp", "ube", "touched by dibella"))
                    male = any(token in body for token in ("himbo", "sam body", "shape atlas", "sos body", "tng body"))
                    category = "Female" if female and not male else "Male" if male and not female else "Unknown"
                    found[category].add(name)
            except (ET.ParseError, OSError):
                continue
        return {key: sorted(values) for key, values in found.items()}


# =====================================================================
# 3. TARGET POOLS & CONFIG EXPORTERS (SPID, SKYPATCHER, OBODY NG)
# =====================================================================

TARGET_POOLS = {
    'Tavern Servers': {"spid_strings": "ActorTypeNPC", "spid_forms": '0xDEE93~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|0DEE93', "obody_faction": 'JobInnServer'},
    'Innkeepers': {"spid_strings": "ActorTypeNPC", "spid_forms": '0x5091B~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|05091B', "obody_faction": 'JobInnkeeperFaction'},
    'Bandits': {"spid_strings": "ActorTypeNPC", "spid_forms": '0x1BCC0~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|01BCC0', "obody_faction": 'BanditFaction'},
    'Vampires': {"spid_strings": "ActorTypeNPC", "spid_forms": '0x27242~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|027242', "obody_faction": 'VampireFaction'},
    'Mages & Warlocks': {"spid_strings": "ActorTypeNPC", "spid_forms": '0x26724~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|026724', "obody_faction": 'WarlockFaction'},
    'Nobles (Road Encounters)': {"spid_strings": "ActorTypeNPC", "spid_forms": '0x102D60~Skyrim.esm',
        "skypatcher_filter": 'filterByFactions=Skyrim.esm|102D60', "obody_faction": 'WERoad02NobleFaction'},
    'All NPCs (Global)': {"spid_strings": "ActorTypeNPC", "spid_forms": 'NONE',
        "skypatcher_filter": 'filterByKeywords=Skyrim.esm|013794', "obody_faction": None},
}

@dataclass
class DistributionRule:
    record: SkyrimRecord
    pool_name: str
    chance: int
    obody_preset: str
    force_orefit: bool
    gender: str = "Female"
    obody_preset_male: str = "(None / Random)"

    def __post_init__(self):
        if self.gender not in ("Female", "Male", "Both"):
            raise ValueError("Gender must be Female, Male, or Both.")

    def preset_summary(self):
        choices = []
        if self.gender in ("Female", "Both"):
            choices.append(f"Female: {self.obody_preset}")
        if self.gender in ("Male", "Both"):
            choices.append(f"Male: {self.obody_preset_male}")
        return "; ".join(choices)

class ConfigExporters:
    @staticmethod
    def generate_spid(rules: List[DistributionRule]) -> str:
        lines = [
            "; Generated by SkyFit Studio (SPID Outfit & Armor Distributor)",
            "; Syntax: Outfit/Item = FormOrEditorID|StringFilters|FormFilters|LevelFilters|TraitFilters|CountOrPackage|Chance",
            ""
        ]
        for r in rules:
            pool = TARGET_POOLS[r.pool_name]
            traits = {"Female": "F", "Male": "M", "Both": "NONE"}[r.gender]
            rec_key = "Outfit" if r.record.record_type == "OTFT" else "Item"
            form_ref = f"{r.record.form_id_hex}~{r.record.plugin_name}"
            line = (
                f"; {r.record.editor_id} -> {r.pool_name} / {r.gender} ({r.chance}% Chance)\n"
                f"{rec_key} = {form_ref}|{pool['spid_strings']}|{pool['spid_forms']}|NONE|{traits}|NONE|{r.chance}"
            )
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def generate_skypatcher(rules: List[DistributionRule], strict_100_only: bool = False) -> str:
        lines = [
            "; Generated by SkyFit Studio (SkyPatcher NPC Outfit Config)",
            "; Place in: Data/SKSE/Plugins/SkyPatcher/npc/SkyFitStudio/SkyFit.ini",
            "; NOTE: SkyPatcher's 'outfitDefault' applies deterministically (100%) to matching NPCs.",
            ";       For randomized percentage rolls (<100%), use the SPID (_DISTR.ini) output.",
            ""
        ]
        for r in rules:
            if r.record.record_type != "OTFT":
                lines.append(
                    f"; [Skipped ARMO] {r.record.editor_id}: SkyPatcher outfitDefault requires an OTFT record."
                )
                continue

            if strict_100_only and r.chance < 100:
                lines.append(
                    f"; [Skipped <100% Rule] {r.record.editor_id} ({r.chance}% -> {r.pool_name}): "
                    f"Handled via SPID for random roll."
                )
                continue

            pool = TARGET_POOLS[r.pool_name]
            clean_hex = r.record.form_id_hex.replace("0x", "")
            filters = [pool["skypatcher_filter"]] if pool["skypatcher_filter"] else []
            if r.gender != "Both":
                filters.append(f"filterByGender={r.gender.lower()}")
            filters.append(f"outfitDefault={r.record.plugin_name}|{clean_hex}")
            chance_note = (
                "100% Static Assignment"
                if r.chance == 100
                else f"Forced 100% Static Override (SPID Chance was set to {r.chance}%)"
            )
            line = (
                f"; {r.record.editor_id} -> {r.pool_name} / {r.gender} [{chance_note}]\n"
                + ":".join(filters)
            )
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def default_obody_schema() -> Dict[str, Any]:
        """Full standard OBody NG JSON schema so existing keys are never lost."""
        return {
            "npcFormID": {},
            "npc": {},
            "factionFemale": {},
            "factionMale": {},
            "npcPluginFemale": {},
            "npcPluginMale": {},
            "raceFemale": {},
            "raceMale": {},
            "blacklistedNpcs": [],
            "blacklistedNpcsFormID": {},
            "blacklistedNpcsPluginFemale": [],
            "blacklistedNpcsPluginMale": [],
            "blacklistedRacesFemale": ["ElderRace"],
            "blacklistedRacesMale": ["ElderRace"],
            "blacklistedOutfitsFromORefit": [],
            "blacklistedOutfitsFromORefitFormID": {},
            "outfitsForceRefit": [],
            "outfitsForceRefitFormID": {},
            "blacklistedPresetsFromRandomDistribution": ["-Zeroed Sliders-"],
            "blacklistedPresetsShowInOBodyMenu": True
        }

    @staticmethod
    def build_obody_dict(rules: List[DistributionRule], base_config: Dict[str, Any] = None) -> Dict[str, Any]:
        config = ConfigExporters.default_obody_schema()
        if base_config is not None:
            ConfigExporters.validate_obody_config(base_config)
            config.update(deepcopy(base_config))

        config.setdefault("factionFemale", {})
        config.setdefault("outfitsForceRefit", [])
        config.setdefault("outfitsForceRefitFormID", {})

        for r in rules:
            pool = TARGET_POOLS[r.pool_name]
            faction_key = pool["obody_faction"]
            for sex, preset in (("Female", r.obody_preset), ("Male", r.obody_preset_male)):
                if faction_key and r.gender in (sex, "Both") and preset and preset != "(None / Random)":
                    category = config.setdefault("faction" + sex, {})
                    category.setdefault(faction_key, [])
                    if preset not in category[faction_key]:
                        category[faction_key].append(preset)

            if r.force_orefit:
                # OBody checks worn ARMO IDs, not the containing OTFT record or its EditorID.
                references = (((r.record.plugin_name, r.record.form_id_hex.removeprefix("0x")),)
                              if r.record.record_type == "ARMO" else r.record.refit_items)
                for plugin_name, local_id in references:
                    values = config["outfitsForceRefitFormID"].setdefault(plugin_name, [])
                    if local_id not in values:
                        values.append(local_id)

        return config

    @staticmethod
    def validate_obody_config(config: Any) -> None:
        if not isinstance(config, dict):
            raise ValueError("OBody configuration must be a JSON object.")
        for key, default in ConfigExporters.default_obody_schema().items():
            if key in config and not isinstance(config[key], type(default)):
                raise ValueError(f"OBody field '{key}' must be a {type(default).__name__}.")
        # Validate the nested lists that the merger appends to, before changing anything.
        for key in ("factionFemale", "factionMale", "outfitsForceRefitFormID"):
            for entry, values in config.get(key, {}).items():
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"OBody field '{key}.{entry}' must be a list of strings.")
        if any(not isinstance(value, str) for value in config.get("outfitsForceRefit", [])):
            raise ValueError("OBody field 'outfitsForceRefit' must be a list of strings.")

    @staticmethod
    def generate_obody_json(rules: List[DistributionRule], base_config: Dict[str, Any] = None) -> str:
        return json.dumps(ConfigExporters.build_obody_dict(rules, base_config), indent=4)


# =====================================================================
# 4. PYQT6 DESKTOP STUDIO GUI
# =====================================================================

class SkyFitWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SkyFit Studio v1.0.1 — Automatic SPID, SkyPatcher & OBody Outfit Distributor")
        self.resize(1480, 900)
        self.setMinimumSize(1160, 820)
        icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "SkyFitStudio.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.records: List[SkyrimRecord] = []
        self.rules: List[DistributionRule] = []
        self.presets: List[str] = list(DEFAULT_PRESETS)
        self.male_presets: List[str] = list(DEFAULT_PRESETS)

        self._apply_dark_theme()
        self._build_ui()
        self.refresh_rules_and_previews()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #181824; color: #E2E2EC; font-family: 'Segoe UI', sans-serif; font-size: 13px; }
            QGroupBox { border: 1px solid #3A3A52; border-radius: 6px; margin-top: 8px; padding-top: 10px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #A29BFE; }
            QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableWidget {
                background-color: #232334; border: 1px solid #3A3A52; border-radius: 5px; padding: 5px; color: #FFFFFF;
            }
            QComboBox:disabled { color: #77778C; background-color: #1D1D2C; }
            QPushButton {
                background-color: #6C5CE7; color: #FFFFFF; border: none; border-radius: 5px;
                padding: 7px 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #8172F3; }
            QHeaderView::section { background-color: #2B2B40; color: #B8B8D0; padding: 6px; border: 1px solid #3A3A52; font-weight: bold; }
            QTabWidget::pane { border: 1px solid #3A3A52; border-radius: 5px; }
            QTabBar::tab { background-color: #232334; padding: 7px 12px; margin-right: 2px; border-top-left-radius: 5px; border-top-right-radius: 5px; }
            QTabBar::tab:selected { background-color: #6C5CE7; font-weight: bold; }
            QToolTip {
                background-color: #232334; color: #FFFFFF; border: 1px solid #6C5CE7;
                border-radius: 4px; padding: 6px 10px; font-family: 'Segoe UI', sans-serif; font-size: 12px;
            }
        """)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)

        # Top Bar: Folder Selector
        top_bar = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select Skyrim 'Data' folder or Mod Organizer 2 'mods' directory...")
        browse_btn = QPushButton("Browse Folder && Scan Plugins")
        browse_btn.clicked.connect(self.scan_directory)
        top_bar.addWidget(QLabel("Mod / Data Directory:"))
        top_bar.addWidget(self.path_input, stretch=1)
        top_bar.addWidget(browse_btn)
        main_layout.addLayout(top_bar)

        # Filter Bar
        filter_bar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter extracted Outfits / Armors by EditorID or Plugin name...")
        self.search_input.textChanged.connect(self.refresh_record_table)
        self.only_otft_chk = QCheckBox("Show Only Complete Outfits (OTFT)")
        self.only_otft_chk.setChecked(True)
        self.only_otft_chk.stateChanged.connect(self.refresh_record_table)
        filter_bar.addWidget(self.search_input, stretch=1)
        filter_bar.addWidget(self.only_otft_chk)
        main_layout.addLayout(filter_bar)

        # Center Splitter (Balanced 50/50 layout so right preview pane is never cramped)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ---------------- LEFT PANEL ----------------
        left_widget = QWidget()
        left_widget.setMinimumWidth(600)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 4, 0)

        self.record_table = QTableWidget(0, 4)
        self.record_table.setHorizontalHeaderLabels(["Plugin", "Type", "FormID", "EditorID"])
        self.record_table.setColumnWidth(0, 180)
        self.record_table.horizontalHeader().setMinimumSectionSize(65)
        self.record_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.record_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.record_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        left_layout.addWidget(QLabel("1. Extracted Outfits & Armors (Select rows to assign):"))
        left_layout.addWidget(self.record_table, stretch=3)

        rule_group = QGroupBox("Rule Assignment Settings")
        rule_grid = QGridLayout(rule_group)
        rule_grid.setColumnStretch(1, 1)
        rule_grid.setColumnStretch(3, 1)

        self.pool_combo = QComboBox()
        self.pool_combo.addItems(list(TARGET_POOLS))
        self.gender_combo = QComboBox()
        self.gender_combo.addItems(["Female", "Male", "Both"])
        self.chance_spin = QSpinBox()
        self.chance_spin.setRange(1, 100)
        self.chance_spin.setValue(25)
        self.chance_spin.setSuffix("%")
        self.chance_spin.setMinimumWidth(95)
        self.chance_spin.setToolTip("SPID rolls per NPC. SkyPatcher applies outfits at 100% or skips lower-chance rules.")

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(self.presets)
        self.male_preset_combo = QComboBox()
        self.male_preset_combo.addItems(self.male_presets)
        for combo in (self.pool_combo, self.preset_combo, self.male_preset_combo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(18)
            combo.view().setMinimumWidth(420)
            combo.currentTextChanged.connect(lambda text, control=combo: control.setToolTip(text))
        self.gender_combo.setMinimumWidth(110)

        self.skypatcher_mode_combo = QComboBox()
        self.skypatcher_mode_combo.addItems(["Force 100% (static)", "Only export 100% rules"])
        self.skypatcher_mode_combo.setToolTip("Static mode applies every matching outfit at 100%. Strict mode skips rules whose SPID chance is below 100%.")
        self.skypatcher_mode_combo.currentIndexChanged.connect(self.refresh_rules_and_previews)
        self.orefit_chk = QCheckBox("Force OBody ORefit")
        self.orefit_chk.setToolTip("Force refit for verified armor pieces in the selected outfit, including nested item lists. Unresolved items are skipped and reported below the previews.")
        self.orefit_chk.setChecked(True)
        add_rule_btn = QPushButton("+ Add Selected to Distribution")
        add_rule_btn.clicked.connect(self.add_selected_rules)
        self.target_note = QLabel()
        self.target_note.setWordWrap(True)

        rule_grid.addWidget(QLabel("Target pool:"), 0, 0)
        rule_grid.addWidget(self.pool_combo, 0, 1, 1, 3)
        rule_grid.addWidget(QLabel("Target:"), 1, 0)
        rule_grid.addWidget(self.gender_combo, 1, 1)
        rule_grid.addWidget(QLabel("SPID chance:"), 1, 2)
        rule_grid.addWidget(self.chance_spin, 1, 3)
        rule_grid.addWidget(QLabel("Female preset:"), 2, 0)
        rule_grid.addWidget(self.preset_combo, 2, 1, 1, 3)
        rule_grid.addWidget(QLabel("Male preset:"), 3, 0)
        rule_grid.addWidget(self.male_preset_combo, 3, 1, 1, 3)
        rule_grid.addWidget(QLabel("SkyPatcher:"), 4, 0)
        rule_grid.addWidget(self.skypatcher_mode_combo, 4, 1, 1, 3)
        rule_grid.addWidget(self.orefit_chk, 5, 0, 1, 2)
        rule_grid.addWidget(add_rule_btn, 5, 2, 1, 2)
        rule_grid.addWidget(self.target_note, 6, 0, 1, 4)
        self.gender_combo.currentTextChanged.connect(self.update_target_controls)
        self.pool_combo.currentTextChanged.connect(self.update_target_controls)
        self.update_target_controls()

        left_layout.addWidget(rule_group)

        # Active Rules Table
        left_layout.addWidget(QLabel("2. Active Distribution Rules:"))
        self.rules_table = QTableWidget(0, 6)
        self.rules_table.setHorizontalHeaderLabels(["EditorID", "Plugin", "Target NPC Pool", "SPID %", "OBody Presets", "Target"])
        self.rules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setMinimumSectionSize(70)
        self.rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        left_layout.addWidget(self.rules_table, stretch=2)

        clear_btn = QPushButton("Clear All Rules")
        clear_btn.setStyleSheet("background-color: #D63031;")
        clear_btn.clicked.connect(self.clear_rules)
        left_layout.addWidget(clear_btn)

        # ---------------- RIGHT PANEL ----------------
        right_widget = QWidget()
        right_widget.setMinimumWidth(480)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 0, 0, 0)

        mono_font = QFont("Consolas", 10)
        mono_font.setStyleHint(QFont.StyleHint.Monospace)

        self.tabs = QTabWidget()
        self.spid_preview = QTextEdit()
        self.skypatcher_preview = QTextEdit()
        self.obody_preview = QTextEdit()

        for editor in (self.spid_preview, self.skypatcher_preview, self.obody_preview):
            editor.setFont(mono_font)
            editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        self.tabs.addTab(self.spid_preview, "SPID (_DISTR.ini)")
        self.tabs.addTab(self.skypatcher_preview, "SkyPatcher (.ini)")
        self.tabs.addTab(self.obody_preview, "OBody NG (.json)")

        right_layout.addWidget(QLabel("3. Live Generated Configs (Horizontal Scroll Enabled):"))
        right_layout.addWidget(self.tabs, stretch=1)
        self.refit_status = QLabel()
        self.refit_status.setWordWrap(True)
        self.refit_status.setStyleSheet("color: #FDCB6E;")
        self.refit_status.hide()
        right_layout.addWidget(self.refit_status)

        export_btn = QPushButton("Export All Configs")
        export_btn.setStyleSheet("background-color: #00B894; padding: 10px; font-size: 14px;")
        export_btn.clicked.connect(self.export_files)
        right_layout.addWidget(export_btn)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([760, 720])
        main_layout.addWidget(splitter, stretch=1)

    def update_target_controls(self):
        gender = self.gender_combo.currentText()
        faction = TARGET_POOLS[self.pool_combo.currentText()]["obody_faction"]
        self.preset_combo.setEnabled(bool(faction) and gender in ("Female", "Both"))
        self.male_preset_combo.setEnabled(bool(faction) and gender in ("Male", "Both"))
        if not faction:
            note = "Global outfit rules leave OBody preset distribution unchanged."
        elif gender == "Both":
            note = "Both requires an outfit with male and female models. Presets remain separate."
        else:
            note = "Scan your mods folder to discover compatible BodySlide presets."
        self.target_note.setText(note)

    def scan_directory(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Skyrim Data or MO2 Mods Folder")
        if not folder:
            return
        self._run_scan_on_path(Path(folder))

    def _run_scan_on_path(self, root_path: Path):
        self.path_input.setText(str(root_path))

        # 1. Scan Plugins (.esp, .esm, .esl)
        self.records.clear()
        vanilla_masters = {"skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm"}
        all_plugins = [p for p in root_path.rglob("*")
                       if p.suffix.lower() in (".esp", ".esl", ".esm")]
        plugins = [p for p in all_plugins if p.name.lower() not in vanilla_masters]
        resolver = RefitResolver(all_plugins)
        for plugin in plugins:
            self.records.extend(SkyrimPluginParser.parse_plugin(plugin, resolver))

        # 2. Scan BodySlide Presets (FIX #3: Always reset presets first so stale presets never linger)
        found = BodySlideScanner.scan_presets_by_gender(root_path)
        self.presets = list(DEFAULT_PRESETS) + [p for p in found["Female"] if p not in DEFAULT_PRESETS]
        self.male_presets = list(DEFAULT_PRESETS) + [p for p in found["Male"] if p not in DEFAULT_PRESETS]
        self.preset_combo.clear()
        self.preset_combo.addItems(self.presets)
        self.male_preset_combo.clear()
        self.male_preset_combo.addItems(self.male_presets)

        self.refresh_record_table()
        QMessageBox.information(
            self, "Scan Complete",
            f"Scanned {len(plugins)} plugins.\n"
            f"Found {len(self.records)} outfit/armor records.\n"
            f"BodySlide presets: {len(found['Female'])} female, {len(found['Male'])} male.\n"
            f"{len(found['Unknown'])} presets with unrecognized body metadata were omitted."
        )

    def refresh_record_table(self):
        query = self.search_input.text().lower().strip()
        only_otft = self.only_otft_chk.isChecked()

        filtered = [
            r for r in self.records
            if (not only_otft or r.record_type == "OTFT")
            and (query in r.editor_id.lower() or query in r.plugin_name.lower())
        ]

        self.record_table.setRowCount(len(filtered))
        for row_idx, rec in enumerate(filtered):
            if rec.full_name and rec.full_name.isprintable():
                tooltip_text = (
                    f"Outfit / EditorID: {rec.editor_id}\n"
                    f"Display Name: {rec.full_name}\n"
                    f"Plugin: {rec.plugin_name}\n"
                    f"FormID: {rec.form_id_hex} ({rec.record_type})"
                )
            else:
                tooltip_text = (
                    f"Outfit / EditorID: {rec.editor_id}\n"
                    f"Plugin: {rec.plugin_name}\n"
                    f"FormID: {rec.form_id_hex} ({rec.record_type})"
                )

            p_item = QTableWidgetItem(rec.plugin_name)
            p_item.setToolTip(tooltip_text)
            self.record_table.setItem(row_idx, 0, p_item)

            t_item = QTableWidgetItem(rec.record_type)
            t_item.setToolTip(tooltip_text)
            self.record_table.setItem(row_idx, 1, t_item)

            f_item = QTableWidgetItem(rec.form_id_hex)
            f_item.setToolTip(tooltip_text)
            self.record_table.setItem(row_idx, 2, f_item)

            edid_item = QTableWidgetItem(rec.editor_id)
            edid_item.setData(Qt.ItemDataRole.UserRole, rec)
            edid_item.setToolTip(tooltip_text)
            self.record_table.setItem(row_idx, 3, edid_item)

    def add_selected_rules(self):
        selected_rows = {item.row() for item in self.record_table.selectedItems()}
        if not selected_rows:
            QMessageBox.warning(self, "No Outfit Selected", "Highlight one or more outfits in the top table first.")
            return

        for row in sorted(selected_rows):
            rec: SkyrimRecord = self.record_table.item(row, 3).data(Qt.ItemDataRole.UserRole)
            rule = DistributionRule(
                record=rec,
                pool_name=self.pool_combo.currentText(),
                chance=self.chance_spin.value(),
                obody_preset=self.preset_combo.currentText() if self.preset_combo.isEnabled() else "(None / Random)",
                force_orefit=self.orefit_chk.isChecked(),
                gender=self.gender_combo.currentText(),
                obody_preset_male=self.male_preset_combo.currentText() if self.male_preset_combo.isEnabled() else "(None / Random)"
            )
            self.rules.append(rule)

        self.refresh_rules_and_previews()

    def clear_rules(self):
        self.rules.clear()
        self.refresh_rules_and_previews()

    def refresh_rules_and_previews(self):
        self.rules_table.setRowCount(len(self.rules))
        for i, r in enumerate(self.rules):
            rec = r.record
            if rec.full_name and rec.full_name.isprintable():
                rule_tooltip = (
                    f"Outfit / EditorID: {rec.editor_id}\n"
                    f"Display Name: {rec.full_name}\n"
                    f"Plugin: {rec.plugin_name} ({rec.form_id_hex})\n"
                    f"Target Pool: {r.pool_name} / {r.gender}\n"
                    f"SPID Chance: {r.chance}%\n"
                    f"OBody Presets: {r.preset_summary()}"
                )
            else:
                rule_tooltip = (
                    f"Outfit / EditorID: {rec.editor_id}\n"
                    f"Plugin: {rec.plugin_name} ({rec.form_id_hex})\n"
                    f"Target Pool: {r.pool_name} / {r.gender}\n"
                    f"SPID Chance: {r.chance}%\n"
                    f"OBody Presets: {r.preset_summary()}"
                )

            edid_item = QTableWidgetItem(rec.editor_id)
            edid_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 0, edid_item)

            p_item = QTableWidgetItem(rec.plugin_name)
            p_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 1, p_item)

            pool_item = QTableWidgetItem(r.pool_name)
            pool_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 2, pool_item)

            chance_item = QTableWidgetItem(f"{r.chance}%")
            chance_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 3, chance_item)

            preset_item = QTableWidgetItem(r.preset_summary())
            preset_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 4, preset_item)
            target_item = QTableWidgetItem(r.gender)
            target_item.setToolTip(rule_tooltip)
            self.rules_table.setItem(i, 5, target_item)

        refit_notes = []
        for rule in self.rules:
            if rule.force_orefit and rule.record.record_type == "OTFT":
                notes = rule.record.refit_notes
                if not notes and not rule.record.refit_items:
                    notes = ("No armor pieces could be verified; Force ORefit was omitted for this outfit.",)
                refit_notes.extend(f"{rule.record.editor_id}: {note}" for note in notes)
        self.refit_status.setVisible(bool(refit_notes))
        self.refit_status.setText("Some outfit items could not be verified. Force ORefit skips them; hover here for details.")
        self.refit_status.setToolTip("\n".join(dict.fromkeys(refit_notes)))

        strict_100 = self.skypatcher_mode_combo.currentIndex() == 1
        self.spid_preview.setPlainText(ConfigExporters.generate_spid(self.rules))
        self.skypatcher_preview.setPlainText(ConfigExporters.generate_skypatcher(self.rules, strict_100_only=strict_100))
        self.obody_preview.setPlainText(ConfigExporters.generate_obody_json(self.rules))

    def export_files(self):
        if not self.rules:
            QMessageBox.warning(self, "Empty Rules", "Add at least one outfit distribution rule before exporting.")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Select Output Folder (e.g., an empty MO2 Mod folder)")
        if not out_dir:
            return

        base = Path(out_dir)
        spid_path = base / "SkyFitStudio_DISTR.ini"
        skypatcher_dir = base / "SKSE" / "Plugins" / "SkyPatcher" / "npc" / "SkyFitStudio"
        obody_dir = base / "SKSE" / "Plugins"
        obody_path = obody_dir / "OBody_presetDistributionConfig.json"
        obody_status = "Created new OBody_presetDistributionConfig.json"
        obody_text = self.obody_preview.toPlainText()
        backup_needed = False

        if obody_path.exists():
            msg = QMessageBox(self)
            msg.setWindowTitle("Existing OBody Config Detected")
            msg.setText(
                f"An existing OBody configuration was found at:\n{obody_path}\n\n"
                "How would you like SkyFit Studio to handle it?"
            )
            merge_btn = msg.addButton("Smart Merge (+ Backup)", QMessageBox.ButtonRole.AcceptRole)
            overwrite_btn = msg.addButton("Overwrite (+ Backup)", QMessageBox.ButtonRole.DestructiveRole)
            skip_btn = msg.addButton("Skip OBody Export", QMessageBox.ButtonRole.RejectRole)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked == skip_btn:
                obody_text = None
                obody_status = "Skipped OBody_presetDistributionConfig.json (existing file untouched)"
            elif clicked == merge_btn:
                try:
                    existing_data = json.loads(obody_path.read_text(encoding="utf-8-sig"))
                    # JSON null is not an empty config; reject it before the optional-base API.
                    ConfigExporters.validate_obody_config(existing_data)
                    obody_text = ConfigExporters.generate_obody_json(self.rules, base_config=existing_data)
                except (OSError, ValueError, UnicodeError) as error:
                    QMessageBox.warning(self, "OBody Merge Failed",
                        f"No configs were exported. The existing OBody file was left untouched.\n\n{error}\n\n"
                        "Correct the existing JSON, or explicitly choose Overwrite to replace it.")
                    return
                backup_needed = True
                obody_status = "Merged into existing OBody JSON"
            elif clicked == overwrite_btn:
                backup_needed = True
                obody_status = "Overwrote OBody JSON"
            else:
                return  # Closing the choice dialog cancels the export.

        # Preflight OBody before writing any config, including editable preview text.
        if obody_text is not None:
            try:
                ConfigExporters.validate_obody_config(json.loads(obody_text))
            except (ValueError, UnicodeError) as error:
                QMessageBox.warning(self, "Invalid OBody Config", f"No configs were exported.\n\n{error}")
                return

        try:
            if backup_needed:
                backup_path = self._backup_obody_config(obody_path)
                obody_status += f" (Backup: {backup_path.name})"
            skypatcher_dir.mkdir(parents=True, exist_ok=True)
            obody_dir.mkdir(parents=True, exist_ok=True)
            spid_path.write_text(self.spid_preview.toPlainText(), encoding="utf-8")
            (skypatcher_dir / "SkyFit.ini").write_text(self.skypatcher_preview.toPlainText(), encoding="utf-8")
            if obody_text is not None:
                obody_path.write_text(obody_text, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Export Failed",
                f"Export could not finish; some files may have been written.\n\n{error}")
            return

        QMessageBox.information(
            self, "Export Successful",
            f"Exported mod folder structure to:\n{base}\n\n"
            f"• {spid_path.name}\n"
            f"• SKSE/Plugins/SkyPatcher/npc/SkyFitStudio/SkyFit.ini\n"
            f"• {obody_status}"
        )

    @staticmethod
    def _backup_obody_config(path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        suffix = 0
        while True:
            unique = f"_{suffix}" if suffix else ""
            backup = path.with_name(f"{path.stem}.{stamp}{unique}.bak")
            try:
                destination = backup.open("xb")
            except FileExistsError:
                suffix += 1
                continue
            try:
                with destination, path.open("rb") as source:
                    shutil.copyfileobj(source, destination)
            except OSError:
                backup.unlink(missing_ok=True)
                raise
            return backup

if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SkyFitStudio.1.0")
    app = QApplication(sys.argv)
    win = SkyFitWindow()
    win.show()
    sys.exit(app.exec())
