#!/usr/bin/env python
"""Print the File Geodatabase schema ogrinfo will not show you: subtypes, attribute rules and attachment linkage.

Start with ogrinfo, because two thirds of this problem are already solved.
GDAL's OpenFileGDB driver reads coded and range domains, with their codes,
descriptions, field types and split and merge policies, and since GDAL 3.6 it
reads relationship classes as well. "ogrinfo -json a.gdb" prints both, correctly,
with no licence and no Esri install. Nothing here replaces that.

Three parts of the schema are still missing from it, and they are the parts that
do not survive a move to any other format:

  * SUBTYPES. A subtype code, its name, and the per-subtype field defaults and
    per-subtype domains that override the ones on the feature class. ogrinfo
    -json on a feature class with three subtypes uses the word "subtype" only
    for OGR field subtypes, never for the geodatabase ones, so the three names
    and the six overrides are not in the output at all.
  * ATTRIBUTE RULES. The Arcade expression text, the rule type, which of insert,
    update and delete fire it, the evaluation order and the error message.
  * ATTACHMENT LINKAGE. ogrinfo does report the __ATTACHREL relationship and
    marks it as media, so this is the weakest of the three. What it does not do
    is put the attachment table, the class it hangs off and the two key fields
    on one line, or say whether that attachment table is in the same .gdb.

gdbxray reads GDB_Items and GDB_ItemRelationships through OGR, parses each item's
Definition XML directly, and prints the result or emits JSON.

Attribute rule expressions are real source code, and people hardcode service
tokens in them. Anything that looks like a token, a password or a bearer header
is masked before it is printed or written. --show-secrets turns the masking off
and is OFF by default.

    python gdbxray.py --self-test
    python gdbxray.py water.gdb
    python gdbxray.py water.gdb --json
    python gdbxray.py water.gdb --json --out schema.json --apply

The parsing needs nothing but the standard library. Only reading a real .gdb
needs GDAL/OGR, and that import is lazy, so --self-test runs on a machine that
has never had GDAL on it.

Exit codes: 0 read, 2 a read or write step failed, 64 usage error.
"""

from __future__ import print_function

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The two system tables every item of schema lives in. GDAL's OpenFileGDB driver
# hides them from GetLayerCount but still hands them over by name, which is why
# this tool needs no open option and no particular GDAL build.
ITEMS_TABLE = "GDB_Items"
RELATIONSHIPS_TABLE = "GDB_ItemRelationships"

# Esri's fixed suffix for the table that holds a feature class's attachments.
# The relationship class that joins them is named <class>__ATTACHREL.
ATTACH_SUFFIX = "__ATTACH"

# Root element names this tool knows how to read. Anything else is REPORTED as
# unrecognised rather than dropped, because a schema report that silently loses
# a third of a geodatabase's items is worse than no report.
DATASET_ROOTS = {
    "DEFeatureClassInfo": "feature class",
    "DETableInfo": "table",
}
DOMAIN_ROOTS = {
    "GPCodedValueDomain2": "coded",
    "GPRangeDomain2": "range",
}
RELATIONSHIP_ROOTS = ("DERelationshipClassInfo",)

# What replaces a masked secret. Kept short so a report stays readable.
MASK = "[redacted]"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Query parameters and assignments whose VALUE is a secret. The value runs to
# the first character that cannot be inside one: a quote, an ampersand,
# whitespace, a semicolon or a bracket. Arcade string literals are double quoted
# and query parameters are ampersand separated, so both ends are covered.
SECRET_RE = re.compile(
    r"(?i)\b(token|password|pwd|passwd|apikey|api_key|client_secret|secret|"
    r"access_token|refresh_token)(\s*[=:]\s*)([^\s\"'&;,)<>]+)")

# An Authorization header written out in full. The scheme is kept, the
# credential is not.
BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)(\s+)([A-Za-z0-9._~+/=-]{8,})")

# esriARTEInsert, esriFieldTypeString and friends. Only the tail is interesting.
# Longest first, so esriARTE is tried before esriART and esriARTEInsert does not
# come back as "EInsert".
ESRI_PREFIXES = ("esriRelNotification", "esriRelCardinality", "esriRelKeyRole",
                 "esriRelKeyType", "esriFieldType", "esriARTE", "esriART",
                 "esriMPT", "esriSPT", "esriDT")


# ----------------------------------------------------------------- pure core

def strip_esri(value):
    """Drop the esri prefix from an enumerated value. esriARTEInsert -> Insert."""
    if not value:
        return ""
    for prefix in ESRI_PREFIXES:
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix):]
    return value


def redact(text):
    """Mask anything in a string that looks like a credential.

    Applied to the report on the way out rather than on the way in, so
    --show-secrets can print the same report unmasked without a second parse.
    """
    if not isinstance(text, str):
        return text
    out = SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return BEARER_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, out)


def redact_tree(obj):
    """redact() over every string inside a nested dict or list.

    Walking the whole structure rather than a list of interesting keys is both
    shorter and safer. A token pasted into a domain description, a subtype name
    or an error message is masked by the same three lines that mask one in an
    Arcade expression, and a key added later is covered without being remembered.
    """
    if isinstance(obj, dict):
        return dict((k, redact_tree(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [redact_tree(v) for v in obj]
    return redact(obj)


def parse_xml(text):
    """The Definition XML as an Element, or None when the item carries none.

    Items such as the root folder have an empty Definition, which is normal and
    not an error. A Definition that is present but broken IS an error and is
    raised, because reporting nothing for a feature class that has subtypes is
    the failure this tool exists to prevent.

    A DOCTYPE is refused. The stdlib parser never resolves an EXTERNAL entity,
    so file:///etc/passwd in a Definition fails as an undefined entity and reads
    nothing, but it does expand INTERNAL ones, which is the billion laughs shape.
    No Definition ArcGIS writes carries a DOCTYPE at all, so refusing one closes
    that without a third-party parser, which this tool is not going to grow a
    dependency on to read a local file the operator already owns.
    """
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    # A leading BOM and an <?xml?> declaration are both accepted by
    # ElementTree; leading whitespace, which some ETL tools add, is not.
    text = text.lstrip("\ufeff").strip()
    if not text:
        return None
    prolog = text
    if prolog.startswith("<?") and "?>" in prolog:
        prolog = prolog[prolog.index("?>") + 2:].lstrip()
    if prolog[:9].upper() == "<!DOCTYPE":
        raise ValueError("this Definition carries a DOCTYPE, which ArcGIS never "
                         "writes and which can carry an entity expansion attack")
    return ElementTree.fromstring(text)


def child_text(element, name, default=""):
    """The text of one direct child, or the default.

    "element.find(name) or default" is the version that looks right and is not.
    An Element with no children is FALSY, so an empty <Description></Description>
    and a missing one take different paths through an expression that reads as if
    they took the same one.
    """
    if element is None:
        return default
    found = element.find(name)
    if found is None:
        return default
    return found.text if found.text is not None else default


def child_int(element, name, default=None):
    """The text of one direct child as an int, or the default."""
    raw = child_text(element, name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_domain(root, kind):
    """A coded or range domain, from its GPCodedValueDomain2 or GPRangeDomain2."""
    domain = {
        "item": "domain",
        "name": child_text(root, "DomainName"),
        "domain_type": kind,
        "field_type": strip_esri(child_text(root, "FieldType")),
        "description": child_text(root, "Description"),
        "merge_policy": strip_esri(child_text(root, "MergePolicy")),
        "split_policy": strip_esri(child_text(root, "SplitPolicy")),
    }
    if kind == "coded":
        domain["coded_values"] = [
            {"code": child_text(coded, "Code"), "name": child_text(coded, "Name")}
            for coded in root.iter("CodedValue")]
    else:
        low = child_text(root, "MinValue")
        high = child_text(root, "MaxValue")
        domain["min_value"] = low
        domain["max_value"] = high
        # A range whose ends are equal permits exactly one value. It is a
        # legitimate and deliberate thing to build, and a report that skips it
        # because "the range is empty" hides a constraint the next editor still
        # has to satisfy.
        domain["fixed"] = bool(low != "" and low == high)
    return domain


def parse_rule(rule):
    """One attribute rule, including which events fire it."""
    events = [strip_esri(text.text or "").lower()
              for holder in rule.iter("TriggeringEvents")
              for text in holder.findall("String")]
    fields = [text.text or ""
              for holder in rule.iter("TriggeringFields")
              for text in holder.findall("String")]
    return {
        "id": child_int(rule, "ID"),
        "name": child_text(rule, "Name"),
        "rule_type": strip_esri(child_text(rule, "Type")).lower(),
        "field": child_text(rule, "FieldName"),
        "description": child_text(rule, "Description"),
        "error_number": child_int(rule, "ErrorNumber"),
        "error_message": child_text(rule, "ErrorMessage"),
        "evaluation_order": child_int(rule, "EvaluationOrder"),
        "expression": child_text(rule, "ScriptExpression"),
        "enabled": child_text(rule, "IsEnabled") == "true",
        "user_editable": child_text(rule, "UserEditable") == "true",
        "batch": child_text(rule, "Batch") == "true",
        "excluded_from_client": (
            child_text(rule, "ExcludeFromClientEvaluation") == "true"),
        "references_external_service": (
            child_text(rule, "ReferencesExternalService") == "true"),
        "triggering_fields": fields,
        "on_insert": "insert" in events,
        "on_update": "update" in events,
        "on_delete": "delete" in events,
        "triggers": events,
    }


def parse_dataset(root, kind):
    """A feature class or table: its fields, subtypes and attribute rules.

    The per-subtype override flags are worked out here because both halves of
    the comparison, the class level default and the subtype level one, live in
    this one XML document. Nothing outside needs a second lookup.
    """
    fields = []
    for field in root.iter("GPFieldInfoEx"):
        # A default is carried in one of two elements depending on the field
        # type, and either can legitimately hold "0" or "". Reading them with
        # "a or b" turns a real default of 0 into no default at all.
        text_default = field.find("DefaultValueString")
        numeric_default = field.find("DefaultValueNumeric")
        if text_default is not None:
            default = text_default.text if text_default.text is not None else ""
        elif numeric_default is not None:
            default = (numeric_default.text
                       if numeric_default.text is not None else "")
        else:
            default = None
        fields.append({
            "name": child_text(field, "Name"),
            "field_type": strip_esri(child_text(field, "FieldType")),
            "alias": child_text(field, "AliasName"),
            "domain": child_text(field, "DomainName"),
            "default": default,
        })
    by_name = dict((f["name"], f) for f in fields)

    subtypes = []
    for subtype in root.iter("Subtype"):
        infos = []
        for info in subtype.iter("SubtypeFieldInfo"):
            name = child_text(info, "FieldName")
            default_element = info.find("DefaultValue")
            default = None
            if default_element is not None:
                default = (default_element.text
                           if default_element.text is not None else "")
            domain = child_text(info, "DomainName")
            parent = by_name.get(name, {})
            infos.append({
                "field": name,
                "default": default,
                "domain": domain,
                "class_default": parent.get("default"),
                "class_domain": parent.get("domain", ""),
                "overrides_default": (default is not None
                                      and default != parent.get("default")),
                "overrides_domain": bool(
                    domain and domain != parent.get("domain", "")),
            })
        subtypes.append({
            # A subtype code of 0 is ordinary, and "if code:" drops it.
            "code": child_int(subtype, "SubtypeCode"),
            "name": child_text(subtype, "SubtypeName"),
            "fields": infos,
        })

    return {
        "item": "dataset",
        "name": child_text(root, "Name"),
        "dataset_type": kind,
        "subtype_field": child_text(root, "SubtypeFieldName"),
        "default_subtype": child_int(root, "DefaultSubtypeCode"),
        "globalid_field": child_text(root, "GlobalIDFieldName"),
        "fields": fields,
        "subtypes": subtypes,
        "rules": [parse_rule(r) for r in root.iter("AttributeRule")],
    }


def parse_relationship(root):
    """A relationship class, with the key field on each side."""
    def keys(container):
        out = []
        holder = root.find(container)
        if holder is None:
            return out
        for key in holder.iter("RelationshipClassKey"):
            out.append({"field": child_text(key, "ObjectKeyName"),
                        "role": strip_esri(child_text(key, "KeyRole"))})
        return out

    origin = [name.text or "" for holder in root.iter("OriginClassNames")
              for name in holder.findall("Name")]
    destination = [name.text or ""
                   for holder in root.iter("DestinationClassNames")
                   for name in holder.findall("Name")]
    return {
        "item": "relationship",
        "name": child_text(root, "Name"),
        "origin": origin[0] if origin else "",
        "destination": destination[0] if destination else "",
        "cardinality": strip_esri(child_text(root, "Cardinality")),
        "composite": child_text(root, "IsComposite") == "true",
        "attributed": child_text(root, "IsAttributed") == "true",
        "forward_label": child_text(root, "ForwardPathLabel"),
        "backward_label": child_text(root, "BackwardPathLabel"),
        "origin_keys": keys("OriginClassKeys"),
        "destination_keys": keys("DestinationClassKeys"),
    }


def parse_item(name, type_uuid, definition):
    """One GDB_Items row, dispatched on the root element of its Definition.

    Dispatching on the XML root rather than on the item type UUID keeps a table
    of thirty GUIDs out of this file and reads the answer the XML itself gives.
    An item whose root is none of the five handled here comes back as
    unrecognised, WITH its name, so the report can list it.
    """
    root = parse_xml(definition)
    tag = root.tag if root is not None else ""
    if tag in DATASET_ROOTS:
        parsed = parse_dataset(root, DATASET_ROOTS[tag])
    elif tag in DOMAIN_ROOTS:
        parsed = parse_domain(root, DOMAIN_ROOTS[tag])
    elif tag in RELATIONSHIP_ROOTS:
        parsed = parse_relationship(root)
    else:
        return {"item": "unrecognised", "name": name or "", "root": tag,
                "type_uuid": type_uuid or ""}
    # GDB_Items.Name is the authority. A Definition can carry an empty <Name>,
    # and an item reported under an empty name cannot be looked up again.
    if not parsed.get("name"):
        parsed["name"] = name or ""
    return parsed


def attachment_links(relationships, dataset_names):
    """The attachment linkage, one entry per __ATTACHREL relationship.

    dataset_names is what was found in the same .gdb, so a relationship pointing
    at an attachment table that is not there is reported as missing rather than
    quietly printed as if it worked.
    """
    links = []
    for rel in relationships:
        if not rel["destination"].upper().endswith(ATTACH_SUFFIX):
            continue
        origin_keys = [k["field"] for k in rel["origin_keys"]]
        links.append({
            "name": rel["name"],
            "class": rel["origin"],
            "attach_table": rel["destination"],
            "origin_key": origin_keys[0] if origin_keys else "",
            "foreign_key": origin_keys[1] if len(origin_keys) > 1 else "",
            "composite": rel["composite"],
            "attach_table_present": rel["destination"] in dataset_names,
        })
    return links


def link_domains(domains, datasets, rows):
    """Fill each domain's used_by list from the GDB_ItemRelationships rows.

    The row type for this link is Esri's DomainInDataset, but the type GUID is
    not read and not hardcoded. A row whose destination is a domain and whose
    origin is a dataset in this same geodatabase means exactly one thing, and
    matching on the two ends survives a release that renumbers the type.

    The point of the list is the EMPTY one. A domain no dataset uses is dead
    weight that an ArcGIS Pro catalog pane will happily show you for years.
    """
    domain_by_uuid = dict((d["uuid"], d) for d in domains if d.get("uuid"))
    dataset_by_uuid = dict((d["uuid"], d) for d in datasets if d.get("uuid"))
    for domain in domains:
        domain["used_by"] = []
    for row in rows:
        domain = domain_by_uuid.get(row.get("dest"))
        dataset = dataset_by_uuid.get(row.get("origin"))
        if domain is not None and dataset is not None:
            if dataset["name"] not in domain["used_by"]:
                domain["used_by"].append(dataset["name"])
    return domains


def analyse(items, source="", relationship_rows=()):
    """Group parsed items into the report. The whole decision core ends here."""
    report = {"source": source, "datasets": [], "domains": [],
              "relationships": [], "attachments": [], "unrecognised": []}
    for item in items:
        parsed = parse_item(item.get("name"), item.get("type"),
                            item.get("definition"))
        parsed["uuid"] = item.get("uuid", "")
        if parsed["item"] == "dataset":
            report["datasets"].append(parsed)
        elif parsed["item"] == "domain":
            report["domains"].append(parsed)
        elif parsed["item"] == "relationship":
            report["relationships"].append(parsed)
        else:
            report["unrecognised"].append(parsed)
    names = set(d["name"] for d in report["datasets"])
    report["attachments"] = attachment_links(report["relationships"], names)
    link_domains(report["domains"], report["datasets"], relationship_rows)
    report["counts"] = {
        "items": len(items),
        "datasets": len(report["datasets"]),
        "domains": len(report["domains"]),
        "relationships": len(report["relationships"]),
        "attachments": len(report["attachments"]),
        "unrecognised": len(report["unrecognised"]),
        "subtypes": sum(len(d["subtypes"]) for d in report["datasets"]),
        "rules": sum(len(d["rules"]) for d in report["datasets"]),
    }
    return report


def select(report, name):
    """A copy of the report holding only the items with this name.

    Attachment linkage follows the CLASS as well as the relationship, because
    somebody asking about WaterLines wants to be told where its attachments live
    without knowing the relationship is called WaterLines__ATTACHREL.
    """
    picked = {"source": report["source"]}
    for key in ("datasets", "domains", "relationships", "unrecognised"):
        picked[key] = [i for i in report[key] if i["name"] == name]
    picked["attachments"] = [
        a for a in report["attachments"]
        if name in (a["class"], a["name"], a["attach_table"])]
    picked["counts"] = dict(report["counts"])
    for key in ("datasets", "domains", "relationships", "unrecognised",
                "attachments"):
        picked["counts"][key] = len(picked[key])
    picked["counts"]["items"] = sum(
        len(picked[k]) for k in
        ("datasets", "domains", "relationships", "unrecognised"))
    picked["counts"]["subtypes"] = sum(len(d["subtypes"])
                                       for d in picked["datasets"])
    picked["counts"]["rules"] = sum(len(d["rules"]) for d in picked["datasets"])
    return picked


def show_value(value):
    """A default value as the report prints it. None means there is no default."""
    if value is None:
        return "(none)"
    if value == "":
        return "''"
    return value


def render(report):
    """The report as the lines the CLI prints. Takes a report already masked."""
    counts = report["counts"]
    out = ["gdbxray: %s" % report["source"],
           "%d item(s): %d dataset(s), %d domain(s), %d relationship(s), "
           "%d unrecognised"
           % (counts["items"], counts["datasets"], counts["domains"],
              counts["relationships"], counts["unrecognised"]),
           "%d subtype(s) and %d attribute rule(s), which ogrinfo does not print"
           % (counts["subtypes"], counts["rules"])]

    for dataset in report["datasets"]:
        if not (dataset["subtypes"] or dataset["rules"]):
            continue
        out.append("")
        out.append("%s %s" % (dataset["dataset_type"].upper(), dataset["name"]))
        if dataset["subtypes"]:
            out.append("  subtype field: %s (default subtype %s)"
                       % (dataset["subtype_field"] or "(none)",
                          dataset["default_subtype"]))
        for subtype in dataset["subtypes"]:
            out.append("  subtype %s  %s" % (subtype["code"], subtype["name"]))
            for info in subtype["fields"]:
                note = ""
                if info["overrides_default"]:
                    note += "   <-- overrides the class default %s" % show_value(
                        info["class_default"])
                if info["overrides_domain"]:
                    note += "   <-- overrides the class domain %s" % (
                        info["class_domain"] or "(none)")
                out.append("      %-16s default %-10s domain %s%s"
                           % (info["field"], show_value(info["default"]),
                              info["domain"] or "(none)", note))
        for rule in dataset["rules"]:
            target = " on field %s" % rule["field"] if rule["field"] else ""
            out.append("  attribute rule %s %s (%s)%s"
                       % (rule["id"], rule["name"], rule["rule_type"], target))
            out.append("      triggers: %s   evaluation order %s   %s"
                       % (", ".join(rule["triggers"]) or "(none)",
                          rule["evaluation_order"],
                          "enabled" if rule["enabled"] else "DISABLED"))
            if rule["description"]:
                out.append("      %s" % rule["description"])
            if rule["error_message"]:
                out.append("      error %s: %s"
                           % (rule["error_number"], rule["error_message"]))
            for line in (rule["expression"] or "").splitlines() or [""]:
                out.append("      | %s" % line)

    if report["attachments"]:
        out.append("")
        out.append("ATTACHMENTS")
        for link in report["attachments"]:
            out.append("  %s -> %s  %s -> %s%s%s"
                       % (link["class"], link["attach_table"],
                          link["origin_key"] or "(none)",
                          link["foreign_key"] or "(none)",
                          "  composite" if link["composite"] else "",
                          "" if link["attach_table_present"]
                          else "  <-- attachment table NOT in this .gdb"))

    if report["domains"]:
        out.append("")
        out.append("DOMAINS (ogrinfo prints these too)")
        for domain in report["domains"]:
            if domain["domain_type"] == "coded":
                detail = "%d value(s)" % len(domain["coded_values"])
            elif domain["fixed"]:
                detail = "fixed at %s (min equals max)" % domain["min_value"]
            else:
                detail = "%s to %s" % (domain["min_value"], domain["max_value"])
            users = domain.get("used_by") or []
            out.append("  %-20s %-6s %-9s %-28s %s"
                       % (domain["name"], domain["domain_type"],
                          domain["field_type"], detail,
                          "used by " + ", ".join(users) if users else "UNUSED"))

    if report["relationships"]:
        out.append("")
        out.append("RELATIONSHIPS (ogrinfo prints these too)")
        for rel in report["relationships"]:
            keys = [k["field"] for k in rel["origin_keys"]]
            out.append("  %-24s %s -> %s  %s  %s"
                       % (rel["name"], rel["origin"] or "(none)",
                          rel["destination"] or "(none)", rel["cardinality"],
                          " -> ".join(keys) or "(no keys)"))

    if report["unrecognised"]:
        out.append("")
        out.append("UNRECOGNISED (%d)" % len(report["unrecognised"]))
        for item in report["unrecognised"]:
            out.append("  %-24s %s" % (item["name"] or "(unnamed)",
                                       item["root"] or "(empty definition)"))
    return out


# --------------------------------------------------------------- geodatabase

# The GDB_Items columns this tool reads, and the key each becomes.
ITEM_FIELDS = (("UUID", "uuid"), ("Name", "name"), ("Type", "type"),
               ("Definition", "definition"))

# The GDB_ItemRelationships columns. OriginID and DestID are item UUIDs, which
# is how a domain is tied to the datasets that use it.
RELATIONSHIP_FIELDS = (("OriginID", "origin"), ("DestID", "dest"),
                       ("Type", "type"))


def import_ogr():
    """The osgeo.ogr module, or None when GDAL is not installed.

    Returning None rather than exiting keeps the decision in main, where the
    message can name the interpreter that would have it. Nothing above this
    line imports GDAL, so --self-test runs on a machine that has never had it.
    """
    try:
        from osgeo import ogr
    except ImportError:
        return None
    # GDAL 4 turns exceptions on by default and 3.x warns on stderr until one
    # of these two is called. A tool that prints a schema should not print a
    # FutureWarning above it.
    ogr.UseExceptions()
    return ogr


def read_table(source, table, fields, required):
    """Rows of one system table as dicts, keyed by this tool's own names."""
    layer = source.GetLayerByName(table)
    if layer is None:
        if required:
            raise IOError(
                "%s has no %s table. OpenFileGDB hands that table over by name "
                "even though it hides it from the layer list, so this is not a "
                "File Geodatabase gdbxray can read." % (source.GetName(), table))
        return []
    return [dict((key, feature.GetFieldAsString(column))
                 for column, key in fields)
            for feature in layer]


def read_gdb(path, ogr):
    """(items, relationship rows) read straight out of the two system tables."""
    source = ogr.Open(path)
    if source is None:
        raise IOError("OGR could not open %s. A File Geodatabase is a "
                      "DIRECTORY ending .gdb, not a file." % path)
    return (read_table(source, ITEMS_TABLE, ITEM_FIELDS, True),
            read_table(source, RELATIONSHIPS_TABLE, RELATIONSHIP_FIELDS, False))


# ------------------------------------------------------------------ self-test

# Real Definition XML, copied out of a File Geodatabase built with arcpy, with
# the spatial reference and extent blocks cut out. Nothing below reads either,
# and leaving 1200 characters of projection parameters in would hide what these
# fixtures are for. Everything else is byte for byte what ArcGIS wrote.
#
# r-strings throughout: CatalogPath holds a backslash, and "\W" in a plain
# string is a SyntaxWarning waiting for the next Python release to promote.

FEATURE_CLASS_XML = r"""<DEFeatureClassInfo xsi:type='typens:DEFeatureClassInfo'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:xs='http://www.w3.org/2001/XMLSchema'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\WaterLines</CatalogPath><Name>WaterLines</Name><DatasetType>esriDTFeatureClass</DatasetType><HasOID>true</HasOID><OIDFieldName>OBJECTID</OIDFieldName><GPFieldInfoExs xsi:type='typens:ArrayOfGPFieldInfoEx'><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>OBJECTID</Name><FieldType>esriFieldTypeOID</FieldType><IsNullable>false</IsNullable><Required>true</Required><Editable>false</Editable></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>ASSETTYPE</Name><FieldType>esriFieldTypeSmallInteger</FieldType><DefaultValueNumeric>1</DefaultValueNumeric><IsNullable>true</IsNullable></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>MATERIAL</Name><AliasName>Material</AliasName><DomainName>MaterialCD</DomainName><FieldType>esriFieldTypeString</FieldType><DefaultValueString>PVC</DefaultValueString><IsNullable>true</IsNullable></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>DIAMETER</Name><DomainName>DiameterRG</DomainName><FieldType>esriFieldTypeInteger</FieldType><DefaultValueNumeric>8</DefaultValueNumeric><IsNullable>true</IsNullable></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>CONDITION</Name><FieldType>esriFieldTypeString</FieldType><IsNullable>true</IsNullable></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>GlobalID</Name><FieldType>esriFieldTypeGlobalID</FieldType><IsNullable>false</IsNullable><Required>true</Required><Editable>false</Editable></GPFieldInfoEx></GPFieldInfoExs><HasGlobalID>true</HasGlobalID><GlobalIDFieldName>GlobalID</GlobalIDFieldName><SubtypeFieldName>ASSETTYPE</SubtypeFieldName><DefaultSubtypeCode>1</DefaultSubtypeCode><Subtypes xsi:type='typens:ArrayOfSubtype'><Subtype xsi:type='typens:Subtype'><SubtypeName>Distribution main</SubtypeName><SubtypeCode>1</SubtypeCode><FieldInfos xsi:type='typens:ArrayOfSubtypeFieldInfo'><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>MATERIAL</FieldName><DomainName>MaterialCD</DomainName><DefaultValue xsi:type='xs:string'>PVC</DefaultValue></SubtypeFieldInfo><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>DIAMETER</FieldName><DomainName>DiameterRG</DomainName><DefaultValue xsi:type='xs:double'>8</DefaultValue></SubtypeFieldInfo></FieldInfos></Subtype><Subtype xsi:type='typens:Subtype'><SubtypeName>Transmission main</SubtypeName><SubtypeCode>2</SubtypeCode><FieldInfos xsi:type='typens:ArrayOfSubtypeFieldInfo'><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>MATERIAL</FieldName><DomainName>MaterialCD</DomainName><DefaultValue xsi:type='xs:string'>DI</DefaultValue></SubtypeFieldInfo><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>DIAMETER</FieldName><DomainName>DiameterRG</DomainName><DefaultValue xsi:type='xs:int'>24</DefaultValue></SubtypeFieldInfo></FieldInfos></Subtype><Subtype xsi:type='typens:Subtype'><SubtypeName>Hydrant lateral</SubtypeName><SubtypeCode>3</SubtypeCode><FieldInfos xsi:type='typens:ArrayOfSubtypeFieldInfo'><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>DIAMETER</FieldName><DomainName>DiameterRG</DomainName><DefaultValue xsi:type='xs:double'>8</DefaultValue></SubtypeFieldInfo><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>MATERIAL</FieldName><DomainName>HydrantMaterialCD</DomainName><DefaultValue xsi:type='xs:string'>DI</DefaultValue></SubtypeFieldInfo></FieldInfos></Subtype></Subtypes><AttributeRules xsi:type='typens:ArrayOfAttributeRule'><AttributeRule xsi:type='typens:AttributeRule'><ID>1</ID><Name>CalcCondition</Name><Type>esriARTCalculation</Type><EvaluationOrder>1</EvaluationOrder><FieldName>CONDITION</FieldName><SubtypeCode>-1</SubtypeCode><Description>Set condition from diameter &amp; size</Description><ErrorNumber>-1</ErrorNumber><ErrorMessage></ErrorMessage><UserEditable>true</UserEditable><IsEnabled>true</IsEnabled><ReferencesExternalService>false</ReferencesExternalService><ExcludeFromClientEvaluation>false</ExcludeFromClientEvaluation><ScriptExpression>IIf($feature.DIAMETER &gt;= 12, &quot;MAIN&quot;, &quot;LATERAL&quot;)</ScriptExpression><TriggeringEvents xsi:type='typens:ArrayOfString'><String>esriARTEInsert</String><String>esriARTEUpdate</String></TriggeringEvents><Category>-1</Category><Severity>-1</Severity><Tags></Tags><Batch>false</Batch><TriggeringFields xsi:type='typens:ArrayOfString'></TriggeringFields></AttributeRule><AttributeRule xsi:type='typens:AttributeRule'><ID>2</ID><Name>DiameterPositive</Name><Type>esriARTConstraint</Type><EvaluationOrder>1</EvaluationOrder><FieldName></FieldName><SubtypeCode>-1</SubtypeCode><Description>Diameter must be &gt; 0 &lt;always&gt;</Description><ErrorNumber>1001</ErrorNumber><ErrorMessage>Diameter must be greater than zero</ErrorMessage><UserEditable>true</UserEditable><IsEnabled>true</IsEnabled><ReferencesExternalService>false</ReferencesExternalService><ExcludeFromClientEvaluation>false</ExcludeFromClientEvaluation><ScriptExpression>$feature.DIAMETER &gt; 0</ScriptExpression><TriggeringEvents xsi:type='typens:ArrayOfString'><String>esriARTEInsert</String><String>esriARTEUpdate</String></TriggeringEvents><Category>-1</Category><Severity>-1</Severity><Tags></Tags><Batch>false</Batch><TriggeringFields xsi:type='typens:ArrayOfString'></TriggeringFields></AttributeRule><AttributeRule xsi:type='typens:AttributeRule'><ID>3</ID><Name>StampDiameter</Name><Type>esriARTCalculation</Type><EvaluationOrder>2</EvaluationOrder><FieldName>DIAMETER</FieldName><SubtypeCode>-1</SubtypeCode><Description>Copies the diameter; the service url still carries token=AAPK6f2c9d1e4b7a3f</Description><ErrorNumber>-1</ErrorNumber><ErrorMessage></ErrorMessage><UserEditable>true</UserEditable><IsEnabled>true</IsEnabled><ReferencesExternalService>false</ReferencesExternalService><ExcludeFromClientEvaluation>false</ExcludeFromClientEvaluation><ScriptExpression>var svc = &quot;https://services1.arcgis.com/ExAmPlE/arcgis/rest/services/Assets/FeatureServer/0/query?where=1=1&amp;token=AAPK6f2c9d1e4b7a3f&amp;f=json&quot;; return $feature.DIAMETER</ScriptExpression><TriggeringEvents xsi:type='typens:ArrayOfString'><String>esriARTEInsert</String></TriggeringEvents><Category>-1</Category><Severity>-1</Severity><Tags></Tags><Batch>false</Batch><TriggeringFields xsi:type='typens:ArrayOfString'></TriggeringFields></AttributeRule></AttributeRules></DEFeatureClassInfo>"""

CODED_DOMAIN_XML = r"""<GPCodedValueDomain2 xsi:type='typens:GPCodedValueDomain2'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:xs='http://www.w3.org/2001/XMLSchema'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><DomainName>MaterialCD</DomainName><FieldType>esriFieldTypeString</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy><SplitPolicy>esriSPTDefaultValue</SplitPolicy><Description>Pipe material &lt;all types&gt; &amp; fittings</Description><Owner></Owner><CodedValues xsi:type='typens:ArrayOfCodedValue'><CodedValue xsi:type='typens:CodedValue'><Name>Ductile iron</Name><Code xsi:type='xs:string'>DI</Code></CodedValue><CodedValue xsi:type='typens:CodedValue'><Name>Polyvinyl chloride</Name><Code xsi:type='xs:string'>PVC</Code></CodedValue><CodedValue xsi:type='typens:CodedValue'><Name>Cast iron</Name><Code xsi:type='xs:string'>CI</Code></CodedValue><CodedValue xsi:type='typens:CodedValue'><Name>High density polyethylene</Name><Code xsi:type='xs:string'>HDPE</Code></CodedValue></CodedValues></GPCodedValueDomain2>"""

# The same shape with the description written as a CDATA section holding a bare
# ampersand. ArcGIS itself escapes the character, but a Definition that has been
# through an XML editor, an export and an import, or somebody's sed, comes back
# like this, and it is still well formed XML.
CDATA_DOMAIN_XML = r"""<GPCodedValueDomain2 xsi:type='typens:GPCodedValueDomain2'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><DomainName>UtilityCD</DomainName><FieldType>esriFieldTypeString</FieldType><Description><![CDATA[Water & sewer <mains> only]]></Description><CodedValues xsi:type='typens:ArrayOfCodedValue'><CodedValue xsi:type='typens:CodedValue'><Name>Water &amp; sewer</Name><Code xsi:type='xs:string'>WS</Code></CodedValue></CodedValues></GPCodedValueDomain2>"""

RANGE_DOMAIN_XML = r"""<GPRangeDomain2 xsi:type='typens:GPRangeDomain2'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:xs='http://www.w3.org/2001/XMLSchema'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><DomainName>DiameterRG</DomainName><FieldType>esriFieldTypeInteger</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy><SplitPolicy>esriSPTDefaultValue</SplitPolicy><Description>Nominal diameter in inches</Description><Owner></Owner><MaxValue xsi:type='xs:int'>48</MaxValue><MinValue xsi:type='xs:int'>2</MinValue></GPRangeDomain2>"""

FIXED_RANGE_DOMAIN_XML = r"""<GPRangeDomain2 xsi:type='typens:GPRangeDomain2'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:xs='http://www.w3.org/2001/XMLSchema'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><DomainName>PressureFixed</DomainName><FieldType>esriFieldTypeInteger</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy><SplitPolicy>esriSPTDefaultValue</SplitPolicy><Description>Fixed test pressure</Description><Owner></Owner><MaxValue xsi:type='xs:int'>150</MaxValue><MinValue xsi:type='xs:int'>150</MinValue></GPRangeDomain2>"""

ATTACHREL_XML = r"""<DERelationshipClassInfo xsi:type='typens:DERelationshipClassInfo'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\WaterLines__ATTACHREL</CatalogPath><Name>WaterLines__ATTACHREL</Name><DatasetType>esriDTRelationshipClass</DatasetType><Cardinality>esriRelCardinalityOneToMany</Cardinality><Notification>esriRelNotificationNone</Notification><IsAttributed>false</IsAttributed><IsComposite>true</IsComposite><OriginClassNames xsi:type='typens:Names'><Name>WaterLines</Name></OriginClassNames><DestinationClassNames xsi:type='typens:Names'><Name>WaterLines__ATTACH</Name></DestinationClassNames><KeyType>esriRelKeyTypeSingle</KeyType><ForwardPathLabel>attachment</ForwardPathLabel><BackwardPathLabel>object</BackwardPathLabel><IsReflexive>false</IsReflexive><OriginClassKeys xsi:type='typens:ArrayOfRelationshipClassKey'><RelationshipClassKey xsi:type='typens:RelationshipClassKey'><ObjectKeyName>GlobalID</ObjectKeyName><ClassKeyName></ClassKeyName><KeyRole>esriRelKeyRoleOriginPrimary</KeyRole></RelationshipClassKey><RelationshipClassKey xsi:type='typens:RelationshipClassKey'><ObjectKeyName>REL_GLOBALID</ObjectKeyName><ClassKeyName></ClassKeyName><KeyRole>esriRelKeyRoleOriginForeign</KeyRole></RelationshipClassKey></OriginClassKeys><DestinationClassKeys xsi:type='typens:ArrayOfRelationshipClassKey'></DestinationClassKeys></DERelationshipClassInfo>"""

RELATIONSHIP_XML = r"""<DERelationshipClassInfo xsi:type='typens:DERelationshipClassInfo'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\WaterLines_Inspections</CatalogPath><Name>WaterLines_Inspections</Name><DatasetType>esriDTRelationshipClass</DatasetType><Cardinality>esriRelCardinalityOneToMany</Cardinality><Notification>esriRelNotificationNone</Notification><IsAttributed>false</IsAttributed><IsComposite>false</IsComposite><OriginClassNames xsi:type='typens:Names'><Name>WaterLines</Name></OriginClassNames><DestinationClassNames xsi:type='typens:Names'><Name>InspectionLog</Name></DestinationClassNames><KeyType>esriRelKeyTypeSingle</KeyType><ForwardPathLabel>has inspections</ForwardPathLabel><BackwardPathLabel>inspects</BackwardPathLabel><IsReflexive>false</IsReflexive><OriginClassKeys xsi:type='typens:ArrayOfRelationshipClassKey'><RelationshipClassKey xsi:type='typens:RelationshipClassKey'><ObjectKeyName>GLOBALID</ObjectKeyName><ClassKeyName></ClassKeyName><KeyRole>esriRelKeyRoleOriginPrimary</KeyRole></RelationshipClassKey><RelationshipClassKey xsi:type='typens:RelationshipClassKey'><ObjectKeyName>LINEID</ObjectKeyName><ClassKeyName></ClassKeyName><KeyRole>esriRelKeyRoleOriginForeign</KeyRole></RelationshipClassKey></OriginClassKeys><DestinationClassKeys xsi:type='typens:ArrayOfRelationshipClassKey'></DestinationClassKeys></DERelationshipClassInfo>"""

ATTACH_TABLE_XML = r"""<DETableInfo xsi:type='typens:DETableInfo'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\WaterLines__ATTACH</CatalogPath><Name>WaterLines__ATTACH</Name><DatasetType>esriDTTable</DatasetType><HasOID>true</HasOID><OIDFieldName>ATTACHMENTID</OIDFieldName><GPFieldInfoExs xsi:type='typens:ArrayOfGPFieldInfoEx'><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>ATTACHMENTID</Name><FieldType>esriFieldTypeOID</FieldType></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>REL_GLOBALID</Name><FieldType>esriFieldTypeGUID</FieldType></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>CONTENT_TYPE</Name><FieldType>esriFieldTypeString</FieldType></GPFieldInfoEx></GPFieldInfoExs><Subtypes xsi:type='typens:ArrayOfSubtype'></Subtypes></DETableInfo>"""

# A raster dataset and the workspace item itself. Neither is schema this tool
# reads, and both are in every real .gdb, so both must appear in the report
# rather than be dropped on the floor.
RASTER_XML = r"""<DERasterDataset xsi:type='typens:DERasterDataset'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\Slope_Ned</CatalogPath><Name>Slope_Ned</Name><DatasetType>esriDTRasterDataset</DatasetType></DERasterDataset>"""

WORKSPACE_XML = r"""<DEWorkspace xsi:type='typens:DEWorkspace'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\</CatalogPath><Name></Name><WorkspaceType>esriLocalDatabaseWorkspace</WorkspaceType></DEWorkspace>"""

# A feature class whose subtype code is 0 and whose field default is 0. Both are
# ordinary, and both disappear from a report written with "if code:".
ZERO_SUBTYPE_XML = r"""<DEFeatureClassInfo xsi:type='typens:DEFeatureClassInfo'
 xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
 xmlns:xs='http://www.w3.org/2001/XMLSchema'
 xmlns:typens='http://www.esri.com/schemas/ArcGIS/10.8'><CatalogPath>\Poles</CatalogPath><Name>Poles</Name><DatasetType>esriDTFeatureClass</DatasetType><GPFieldInfoExs xsi:type='typens:ArrayOfGPFieldInfoEx'><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>POLETYPE</Name><FieldType>esriFieldTypeSmallInteger</FieldType><DefaultValueNumeric>0</DefaultValueNumeric></GPFieldInfoEx><GPFieldInfoEx xsi:type='typens:GPFieldInfoEx'><Name>OWNER</Name><FieldType>esriFieldTypeString</FieldType><DefaultValueString></DefaultValueString></GPFieldInfoEx></GPFieldInfoExs><SubtypeFieldName>POLETYPE</SubtypeFieldName><DefaultSubtypeCode>0</DefaultSubtypeCode><Subtypes xsi:type='typens:ArrayOfSubtype'><Subtype xsi:type='typens:Subtype'><SubtypeName>Unknown</SubtypeName><SubtypeCode>0</SubtypeCode><FieldInfos xsi:type='typens:ArrayOfSubtypeFieldInfo'><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>OWNER</FieldName><DomainName></DomainName><DefaultValue xsi:type='xs:string'></DefaultValue></SubtypeFieldInfo><SubtypeFieldInfo xsi:type='typens:SubtypeFieldInfo'><FieldName>POLETYPE</FieldName><DomainName>PoleTypeCD</DomainName></SubtypeFieldInfo></FieldInfos></Subtype></Subtypes></DEFeatureClassInfo>"""

# The UUIDs are the real ones out of the fixture geodatabase. They matter only
# because the domain-to-dataset links in GDB_ItemRelationships are made of them.
UUID_WATERLINES = "{917A8F28-61B9-4755-92CD-55A06064E1EB}"
UUID_MATERIAL = "{31FC1D4A-5328-4C0C-8696-7B41BDDAE628}"
UUID_DIAMETER = "{BEE700C8-F9E4-42AA-AE2D-2600F187DEF0}"
UUID_PRESSURE = "{7166A1B1-47A7-4D28-8A4A-E0D0F3D09CC4}"
UUID_DOMAIN_IN_DATASET = "{17E08ADB-2B31-4DCD-8FDD-DF529E88F843}"


def sample_items():
    """The eight GDB_Items rows the offline half of the self-test works from."""
    return [
        {"uuid": UUID_WATERLINES, "name": "WaterLines",
         "type": "{70737809-852C-4A03-9E22-2CECEA5B9BFA}",
         "definition": FEATURE_CLASS_XML},
        {"uuid": "{FA9A6758-132E-4198-894D-0C2D1F61F736}",
         "name": "WaterLines__ATTACH",
         "type": "{CD06BC3B-789D-4C51-AAFA-A467912B8965}",
         "definition": ATTACH_TABLE_XML},
        {"uuid": UUID_MATERIAL, "name": "MaterialCD",
         "type": "{8C368B12-A12E-4C7E-9638-C9C64E69E98F}",
         "definition": CODED_DOMAIN_XML},
        {"uuid": UUID_DIAMETER, "name": "DiameterRG",
         "type": "{C29DA988-8C3E-45F7-8B5C-18E51EE7BEB4}",
         "definition": RANGE_DOMAIN_XML},
        {"uuid": UUID_PRESSURE, "name": "PressureFixed",
         "type": "{C29DA988-8C3E-45F7-8B5C-18E51EE7BEB4}",
         "definition": FIXED_RANGE_DOMAIN_XML},
        {"uuid": "{807D43E9-F932-47CD-A317-3701DDD570EF}",
         "name": "WaterLines__ATTACHREL",
         "type": "{B606A7E1-FA5B-439C-849C-6E9C2481537B}",
         "definition": ATTACHREL_XML},
        {"uuid": "{8833AABE-4D8C-4BD4-A8F7-7923CF372FB8}",
         "name": "WaterLines_Inspections",
         "type": "{B606A7E1-FA5B-439C-849C-6E9C2481537B}",
         "definition": RELATIONSHIP_XML},
        {"uuid": "{1B52A14B-2F78-4292-9F03-8C8052F46688}", "name": "Workspace",
         "type": "{C673FE0F-7280-404F-8532-20755DD8FC06}",
         "definition": WORKSPACE_XML},
    ]


def sample_relationship_rows():
    """The GDB_ItemRelationships rows that tie two of the domains to WaterLines."""
    return [
        {"origin": UUID_WATERLINES, "dest": UUID_MATERIAL,
         "type": UUID_DOMAIN_IN_DATASET},
        {"origin": UUID_WATERLINES, "dest": UUID_DIAMETER,
         "type": UUID_DOMAIN_IN_DATASET},
    ]


class StubLayer(object):
    """One system table, standing in for an OGR layer.

    OGR is the one thing under this tool that cannot be built out of a string
    constant, so the shape it presents is reproduced here instead: a layer that
    iterates features, and a feature that answers GetFieldAsString by column
    name and returns an empty string for a null. Every assertion about read_gdb
    runs against this, and the same function is then run against a real File
    Geodatabase, which is what the README's worked example is.
    """

    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        for row in self.rows:
            yield StubFeature(row)


class StubFeature(object):
    def __init__(self, row):
        self.row = row

    def GetFieldAsString(self, column):
        return self.row.get(column, "")


class StubSource(object):
    def __init__(self, tables, name="stub.gdb"):
        self.tables = tables
        self.name = name

    def GetName(self):
        return self.name

    def GetLayerByName(self, table):
        rows = self.tables.get(table)
        return None if rows is None else StubLayer(rows)


class NarrowStream(object):
    """A stream that encodes like a redirected Windows console: no UTF-8.

    io.StringIO accepts every character there is, so the failure a real console
    produces cannot be reproduced with one, and a suite built only on StringIO
    reports a report that nobody on Windows can actually print. This raises
    UnicodeEncodeError exactly where the console does, on any platform, so the
    assertions below mean the same thing on Windows and on Linux.
    """

    encoding = "cp1252"

    def __init__(self):
        self.text = ""

    def write(self, chunk):
        chunk.encode(self.encoding)   # the console's own failure, deliberately
        self.text += chunk
        return len(chunk)


class StubOgr(object):
    """Stands in for osgeo.ogr. Open returns a source, or None for an unknown path."""

    def __init__(self, sources):
        self.sources = sources
        self.opened = []

    def Open(self, path):
        self.opened.append(path)
        return self.sources.get(path)


def stub_ogr(prefix=""):
    """A StubOgr carrying the fixture geodatabase, and two broken ones.

    prefix is a real temporary directory when main is being driven end to end,
    because main checks a path against the filesystem before it asks OGR for
    anything, and a stub that answers for a path nobody could ever pass would
    test a code path that cannot be reached.
    """
    def path(name):
        return os.path.join(prefix, name) if prefix else name

    items = [{"UUID": i["uuid"], "Name": i["name"], "Type": i["type"],
              "Definition": i["definition"]} for i in sample_items()]
    rows = [{"OriginID": r["origin"], "DestID": r["dest"], "Type": r["type"]}
            for r in sample_relationship_rows()]
    return StubOgr({
        path("water.gdb"): StubSource({ITEMS_TABLE: items,
                                       RELATIONSHIPS_TABLE: rows}, "water.gdb"),
        # A .gdb whose GDB_ItemRelationships table is missing. Old geodatabases
        # really are like this, and the domain links are the only thing lost.
        path("old.gdb"): StubSource({ITEMS_TABLE: items}, "old.gdb"),
        # Something OGR opened that is not a geodatabase at all.
        path("notagdb.gdb"): StubSource({}, "notagdb.gdb"),
    })


def self_test():
    """Assertions over the parsers, the report, the CLI and the OGR reader.

    Nothing here needs GDAL, a geodatabase, a network or a database. The OGR
    reader is driven through a stub that answers the four calls read_gdb makes,
    and the last block writes into a temporary directory to check that --out
    obeys --apply and that what lands on disk carries no token.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, exception=ValueError):
        """Assert fn raises, and hand the message back so it can be asserted on."""
        try:
            fn()
        except exception as exc:
            check(True, label)
            return str(exc)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)
        return ""

    def capture(fn):
        """(what fn returned, everything it printed to either stream)."""
        buf = io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            result = fn()
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        return result, buf.getvalue()

    print("gdbxray self-test: no GDAL, no geodatabase, no network")
    print("-" * 68)

    report = analyse(sample_items(), "water.gdb", sample_relationship_rows())
    water = report["datasets"][0]
    domains = dict((d["name"], d) for d in report["domains"])

    # ---- the esri enumeration prefixes
    check(strip_esri("esriARTEInsert") == "Insert",
          "esriARTEInsert reads as Insert, not as EInsert  <-- pinned defect")
    check(strip_esri("esriARTCalculation") == "Calculation",
          "esriARTCalculation reads as Calculation")
    check(strip_esri("esriFieldTypeString") == "String",
          "a field type loses its prefix")
    check(strip_esri("esriRelCardinalityOneToMany") == "OneToMany",
          "a cardinality loses the longer of two prefixes that both match")
    check(strip_esri("Something") == "Something",
          "a value with no esri prefix is left alone")
    check(strip_esri("") == "" and strip_esri(None) == "",
          "an absent value reads as the empty string, not None")
    check(strip_esri("esriART") == "esriART",
          "a value that is ONLY a prefix is left alone rather than emptied")

    # ---- masking a credential
    check(redact("token=AAPK6f2c9d1e4b7a3f") == "token=" + MASK,
          "a bare token parameter is masked")
    check("AAPK6f2c9d1e4b7a3f" not in redact(
        "https://x/query?where=1=1&token=AAPK6f2c9d1e4b7a3f&f=json"),
          "a token inside a url is masked")
    check(redact("https://x/q?token=A1&f=json").endswith("&f=json"),
          "the ampersand ends the masked value, so the rest of the url survives")
    check(redact('"token=A1"; return 1') == '"token=' + MASK + '"; return 1',
          "a closing quote ends the masked value")
    check("hunter2" not in redact("password: hunter2"),
          "password with a colon is masked")
    check("hunter2" not in redact("PWD=hunter2"), "pwd is masked")
    check("hunter2" not in redact("ApiKey=hunter2"),
          "apikey is masked whatever its case")
    check("s3kr3t" not in redact("client_secret=s3kr3t"),
          "client_secret is masked")
    check("eyJhbGciOiJIUzI1" not in redact("Authorization: Bearer eyJhbGciOiJIUzI1"),
          "a bearer credential is masked")
    check(redact("Bearer eyJhbGciOiJIUzI1").startswith("Bearer "),
          "and the scheme is kept, so the report still says what it was")
    check(redact("Basic YWxhZGRpbjpvcGVu").endswith(MASK),
          "a basic credential is masked too")
    check(redact("the token is stale") == "the token is stale",
          "the word token in prose is not a credential and is left alone")
    check(redact("bearer up") == "bearer up",
          "a short word after bearer is not a credential")
    check(redact(42) == 42 and redact(None) is None,
          "a value that is not a string passes through unchanged")
    check(redact("$feature.DIAMETER > 0") == "$feature.DIAMETER > 0",
          "an ordinary Arcade expression is not touched")

    tree = redact_tree({"a": ["token=A1", {"b": "pwd=B2"}], "c": 7, "d": None})
    check(tree["a"][0] == "token=" + MASK, "redact_tree masks inside a list")
    check(tree["a"][1]["b"] == "pwd=" + MASK,
          "redact_tree masks inside a dict inside a list")
    check(tree["c"] == 7 and tree["d"] is None,
          "redact_tree leaves non-strings alone")
    check(redact_tree({"a": "x"}) is not None and
          redact_tree({"a": "x"})["a"] == "x",
          "redact_tree returns a new structure, not None")

    # ---- reading the Definition XML at all
    check(parse_xml(None) is None, "a null Definition is not an error")
    check(parse_xml("") is None, "an empty Definition is not an error")
    check(parse_xml("   ") is None, "a whitespace Definition is not an error")
    check(parse_xml("\ufeff" + RANGE_DOMAIN_XML).tag == "GPRangeDomain2",
          "a byte order mark in front of the root element is skipped")
    check(parse_xml("  " + RANGE_DOMAIN_XML).tag == "GPRangeDomain2",
          "leading whitespace in front of the root element is skipped")
    # Whitespace BEFORE the mark, which is what an ETL tool that indents its
    # output produces. ElementTree accepts a leading mark and refuses a leading
    # space, so this one needs the strip to happen and is not the same case.
    check(parse_xml("  \ufeff" + RANGE_DOMAIN_XML).tag == "GPRangeDomain2",
          "whitespace in front of a byte order mark is skipped too")
    check(parse_xml(RANGE_DOMAIN_XML.encode("utf-8")).tag == "GPRangeDomain2",
          "a Definition handed over as bytes is decoded")
    check(parse_xml("<?xml version='1.0' encoding='utf-8'?><GPRangeDomain2/>")
          .tag == "GPRangeDomain2",
          "an xml declaration in front of the root element is accepted")
    raises(lambda: parse_xml(
        "<!DOCTYPE a [<!ENTITY b 'bb'>]><a>&b;</a>"),
        "a DOCTYPE is refused rather than expanded")
    raises(lambda: parse_xml(
        "<?xml version='1.0'?><!DOCTYPE a [<!ENTITY b 'bb'>]><a>&b;</a>"),
        "a DOCTYPE behind an xml declaration is refused too")
    raises(lambda: parse_xml("<a><b></a>"),
           "broken XML raises rather than reporting an empty item",
           ElementTree.ParseError)

    check(child_text(parse_xml(RANGE_DOMAIN_XML), "Owner") == "",
          "an EMPTY element reads as the empty string")
    # ElementTree gives .text of None for an element that is empty AND for one
    # that is absent, so the two cannot be told apart and both take the default.
    # What CAN be got wrong is the element itself: one holding text but no
    # child elements is FALSY, so "if not found: return default" throws away
    # every ordinary value in this file. DomainName is exactly that shape.
    # len() rather than bool(): testing an Element for truth is deprecated and
    # is what this assertion exists to warn people off, so it is not called here.
    owner_element = parse_xml(RANGE_DOMAIN_XML).find("DomainName")
    check(owner_element is not None and len(owner_element) == 0,
          "an element holding text but no children is falsy, which is the trap")
    check(child_text(parse_xml(RANGE_DOMAIN_XML), "DomainName") == "DiameterRG",
          "and it is read by its text anyway, not discarded as empty"
          "  <-- pinned defect")
    check(child_text(parse_xml(RANGE_DOMAIN_XML), "NoSuchElement", "fallback")
          == "fallback", "a missing element reads as the default")
    check(child_text(None, "Anything", "fallback") == "fallback",
          "asking a null element for a child gives the default")
    check(child_int(parse_xml(RANGE_DOMAIN_XML), "MinValue") == 2,
          "an integer element reads as an int")
    check(child_int(parse_xml(RANGE_DOMAIN_XML), "Owner") is None,
          "an empty element is not an int and reads as None")
    check(child_int(parse_xml(RANGE_DOMAIN_XML), "Description") is None,
          "text that is not a number reads as None, not as a traceback")
    check(child_int(parse_xml(RANGE_DOMAIN_XML), "NoSuchElement", -1) == -1,
          "a missing integer element reads as the default")

    # ---- coded domains, which ogrinfo also prints
    material = domains["MaterialCD"]
    check(material["domain_type"] == "coded", "a coded domain is read as coded")
    check(len(material["coded_values"]) == 4,
          "MaterialCD has EXACTLY four coded values")
    check([v["code"] for v in material["coded_values"]]
          == ["DI", "PVC", "CI", "HDPE"],
          "the four codes come back in the order the geodatabase stores them")
    check(material["coded_values"][3]["name"] == "High density polyethylene",
          "each code carries its description")
    check(material["field_type"] == "String", "the domain names its field type")
    check(material["description"] == "Pipe material <all types> & fittings",
          "an escaped ampersand and angle bracket are decoded once, not twice")
    check(material["merge_policy"] == "DefaultValue",
          "the merge policy is read and its prefix dropped")
    check(material["split_policy"] == "DefaultValue", "so is the split policy")
    check("fixed" not in material,
          "a coded domain carries no range flag to be misread")

    # ---- THE PINNED DEFECT: a CDATA description holding a bare ampersand
    cdata = parse_item("UtilityCD", "", CDATA_DOMAIN_XML)
    check(cdata["description"] == "Water & sewer <mains> only",
          "a CDATA description with a bare ampersand parses  <-- pinned defect")
    check("CDATA" not in cdata["description"],
          "and the CDATA markers are not part of the text  <-- pinned defect")
    check(cdata["coded_values"][0]["name"] == "Water & sewer",
          "an escaped ampersand beside it decodes the same way")
    check(len(cdata["coded_values"]) == 1,
          "the CDATA section does not swallow the coded values after it")

    # ---- range domains, including the one that is a single value
    diameter = domains["DiameterRG"]
    check(diameter["domain_type"] == "range", "a range domain is read as range")
    check(diameter["min_value"] == "2" and diameter["max_value"] == "48",
          "both ends of an ordinary range are read")
    check(diameter["fixed"] is False, "an ordinary range is not flagged fixed")
    check("coded_values" not in diameter,
          "a range domain carries no coded value list to be counted")
    pressure = domains["PressureFixed"]
    check(pressure["min_value"] == pressure["max_value"] == "150",
          "PressureFixed really does have min equal to max")
    check(pressure["fixed"] is True,
          "a range whose min EQUALS its max is reported, not skipped as empty"
          "  <-- pinned defect")
    empty_range = parse_item("Empty", "", FIXED_RANGE_DOMAIN_XML.replace(
        "<MaxValue xsi:type='xs:int'>150</MaxValue>", "<MaxValue></MaxValue>")
        .replace("<MinValue xsi:type='xs:int'>150</MinValue>",
                 "<MinValue></MinValue>"))
    check(empty_range["fixed"] is False,
          "two MISSING ends are not a fixed range  <-- pinned defect")
    check(empty_range["min_value"] == "",
          "and the missing end is reported as empty rather than invented")
    # Min above max is a data error somebody should see, not a fixed value. The
    # ends are compared as the strings the XML holds, and "48" sorts above "2",
    # so a >= here would call this range fixed and hide the mistake.
    reversed_range = parse_item("Backwards", "", RANGE_DOMAIN_XML.replace(
        "<MaxValue xsi:type='xs:int'>48</MaxValue>",
        "<MaxValue xsi:type='xs:int'>2</MaxValue>").replace(
        "<MinValue xsi:type='xs:int'>2</MinValue>",
        "<MinValue xsi:type='xs:int'>48</MinValue>"))
    check(reversed_range["min_value"] == "48"
          and reversed_range["max_value"] == "2",
          "a range stored with its ends the wrong way round is read as it is")
    check(reversed_range["fixed"] is False,
          "and is NOT reported as a fixed value  <-- pinned defect")

    # ---- the feature class, its fields and its class level defaults
    check(water["dataset_type"] == "feature class",
          "a DEFeatureClassInfo is a feature class")
    check(water["name"] == "WaterLines", "it carries its name")
    check(len(water["fields"]) == 6, "all six fields are read")
    by_name = dict((f["name"], f) for f in water["fields"])
    check(by_name["MATERIAL"]["default"] == "PVC",
          "a text field default is read")
    check(by_name["MATERIAL"]["domain"] == "MaterialCD",
          "a field level domain is read")
    check(by_name["MATERIAL"]["alias"] == "Material", "so is the field alias")
    check(by_name["DIAMETER"]["default"] == "8",
          "a numeric field default is read")
    check(by_name["CONDITION"]["default"] is None,
          "a field with NO default reads as None, not as an empty string")
    check(by_name["CONDITION"]["domain"] == "",
          "a field with no domain reads as an empty domain name")
    check(by_name["OBJECTID"]["field_type"] == "OID",
          "the field type loses its esri prefix")
    check(water["globalid_field"] == "GlobalID", "the global id field is read")
    check(water["subtype_field"] == "ASSETTYPE", "the subtype field is read")
    check(water["default_subtype"] == 1, "the default subtype code is read")

    # ---- THE HEADLINE: subtypes, and the defaults that override the class
    check(len(water["subtypes"]) == 3, "all three subtypes are read")
    check([s["code"] for s in water["subtypes"]] == [1, 2, 3],
          "each subtype carries its code as an int")
    check([s["name"] for s in water["subtypes"]]
          == ["Distribution main", "Transmission main", "Hydrant lateral"],
          "and its name, which ogrinfo prints nowhere")
    first = dict((f["field"], f) for f in water["subtypes"][0]["fields"])
    check(first["MATERIAL"]["default"] == "PVC",
          "subtype 1 takes the class default PVC")
    check(first["MATERIAL"]["overrides_default"] is False,
          "and is NOT reported as an override, because it is the same value")
    check(first["DIAMETER"]["overrides_domain"] is False,
          "subtype 1 uses the class domain and is not reported as an override")
    second = dict((f["field"], f) for f in water["subtypes"][1]["fields"])
    check(second["MATERIAL"]["default"] == "DI",
          "subtype 2 has its own MATERIAL default")
    check(second["MATERIAL"]["overrides_default"] is True,
          "a subtype default that DIFFERS from the class default is flagged"
          "  <-- pinned defect")
    check(second["MATERIAL"]["class_default"] == "PVC",
          "and the class default it overrides is carried with it")
    check(second["DIAMETER"]["default"] == "24"
          and second["DIAMETER"]["class_default"] == "8",
          "the numeric override carries both values too")
    check(second["DIAMETER"]["overrides_default"] is True,
          "a numeric subtype default that differs is flagged")
    third = dict((f["field"], f) for f in water["subtypes"][2]["fields"])
    check(third["MATERIAL"]["domain"] == "HydrantMaterialCD",
          "subtype 3 has its own MATERIAL domain")
    check(third["MATERIAL"]["overrides_domain"] is True,
          "a subtype domain that differs from the class domain is flagged")
    check(third["MATERIAL"]["class_domain"] == "MaterialCD",
          "and the class domain it narrows is carried with it")
    check(third["DIAMETER"]["overrides_domain"] is False,
          "the other field in the same subtype is not flagged with it")
    check(sum(1 for s in water["subtypes"] for f in s["fields"]
              if f["overrides_default"] or f["overrides_domain"]) == 3,
          "three of the six subtype field entries override something")

    # ---- a subtype code of zero, and a default of zero
    poles = parse_item("Poles", "", ZERO_SUBTYPE_XML)
    check(len(poles["subtypes"]) == 1, "the zero coded subtype is read")
    check(poles["subtypes"][0]["code"] == 0,
          "a subtype whose code is 0 keeps the code 0  <-- pinned defect")
    check(poles["default_subtype"] == 0,
          "a DEFAULT subtype code of 0 survives too  <-- pinned defect")
    zero_fields = dict((f["name"], f) for f in poles["fields"])
    check(zero_fields["POLETYPE"]["default"] == "0",
          "a field default of 0 is a default, not an absent one"
          "  <-- pinned defect")
    check(zero_fields["OWNER"]["default"] == "",
          "a field default of the EMPTY STRING is a default too")
    owner = poles["subtypes"][0]["fields"][0]
    check(owner["default"] == "" and owner["overrides_default"] is False,
          "an empty subtype default matching an empty class default is no "
          "override")
    # A subtype entry that narrows the DOMAIN and leaves the default alone. The
    # element is simply absent, which is not the same as an empty one.
    narrowed = poles["subtypes"][0]["fields"][1]
    check(narrowed["default"] is None,
          "a subtype field with NO DefaultValue element reads as None")
    check(narrowed["overrides_default"] is False,
          "and is not reported as overriding a default it never set")
    check(narrowed["overrides_domain"] is True,
          "while the domain it did set is reported as an override")
    # A subtype entry that sets a default and leaves DomainName EMPTY does not
    # remove the class domain, it simply says nothing about it. Comparing the
    # two names without first checking the subtype set one reports every such
    # entry as narrowing the domain to nothing.
    domained = parse_item("Poles", "", ZERO_SUBTYPE_XML.replace(
        "<Name>OWNER</Name><FieldType>esriFieldTypeString</FieldType>",
        "<Name>OWNER</Name><DomainName>OwnerCD</DomainName>"
        "<FieldType>esriFieldTypeString</FieldType>"))
    class_owner = dict((f["name"], f) for f in domained["fields"])["OWNER"]
    check(class_owner["domain"] == "OwnerCD",
          "the class field carries a domain for the subtype to say nothing "
          "about")
    quiet_entry = domained["subtypes"][0]["fields"][0]
    check(quiet_entry["field"] == "OWNER" and quiet_entry["domain"] == "",
          "and the subtype entry for it leaves DomainName empty")
    check(quiet_entry["overrides_domain"] is False,
          "an EMPTY subtype domain is not an override of the class domain"
          "  <-- pinned defect")

    # ---- attribute rules
    rules = dict((r["name"], r) for r in water["rules"])
    check(len(water["rules"]) == 3, "all three attribute rules are read")
    calc = rules["CalcCondition"]
    check(calc["rule_type"] == "calculation",
          "a calculation rule is read as calculation")
    check(calc["field"] == "CONDITION", "the field it writes is read")
    check(calc["expression"]
          == 'IIf($feature.DIAMETER >= 12, "MAIN", "LATERAL")',
          "the Arcade expression is decoded from its XML entities")
    check(calc["evaluation_order"] == 1, "the evaluation order is read")
    check(calc["enabled"] is True, "an enabled rule is reported enabled")
    check(calc["user_editable"] is True, "the editable flag is read")
    check(calc["batch"] is False, "the batch flag is read")
    check(calc["excluded_from_client"] is False,
          "the client exclusion flag is read")
    check(calc["references_external_service"] is False,
          "the external service flag is read")
    check(calc["description"] == "Set condition from diameter & size",
          "the rule description decodes its escaped ampersand")
    check(calc["triggers"] == ["insert", "update"],
          "both triggering events are read, in order")
    check(calc["on_insert"] is True and calc["on_update"] is True,
          "the insert and update trigger flags are set")
    check(calc["on_delete"] is False,
          "the delete trigger flag is NOT set for a rule that omits it"
          "  <-- pinned defect")
    check(calc["triggering_fields"] == [],
          "an empty triggering field list reads as empty, not as missing")
    constraint = rules["DiameterPositive"]
    check(constraint["rule_type"] == "constraint",
          "a constraint rule is read as constraint")
    check(constraint["field"] == "",
          "a constraint rule with no target field reads as empty")
    check(constraint["error_number"] == 1001, "the error number is read")
    check(constraint["error_message"] == "Diameter must be greater than zero",
          "the error message is read")
    check(constraint["description"] == "Diameter must be > 0 <always>",
          "an escaped angle bracket in a description is decoded")
    check(constraint["id"] == 2, "each rule carries its id")
    stamp = rules["StampDiameter"]
    check(stamp["evaluation_order"] == 2,
          "a second calculation rule carries its own evaluation order")
    check(stamp["triggers"] == ["insert"],
          "a rule that fires on insert alone reads as one trigger")
    check(stamp["on_insert"] is True,
          "its insert flag is on, read from the insert event and no other")
    check(stamp["on_update"] is False,
          "and its update flag is off")

    # ---- the token hardcoded in that rule
    check("AAPK6f2c9d1e4b7a3f" in stamp["expression"],
          "the fixture really does carry a live looking token")
    masked = redact_tree(analyse(sample_items(), "water.gdb"))
    masked_rule = [r for r in masked["datasets"][0]["rules"]
                   if r["name"] == "StampDiameter"][0]
    check("AAPK6f2c9d1e4b7a3f" not in masked_rule["expression"],
          "the token is gone from the masked expression  <-- pinned defect")
    check("AAPK6f2c9d1e4b7a3f" not in masked_rule["description"],
          "and from the rule description beside it  <-- pinned defect")
    check("services1.arcgis.com" in masked_rule["expression"],
          "while the rest of the expression is still readable")
    check("AAPK6f2c9d1e4b7a3f" not in json.dumps(masked),
          "the token appears nowhere in the whole masked report")
    check("AAPK6f2c9d1e4b7a3f" in json.dumps(analyse(sample_items())),
          "and it IS in the unmasked one, so the check above can fail")

    # ---- relationships and attachment linkage
    rels = dict((r["name"], r) for r in report["relationships"])
    check(len(report["relationships"]) == 2, "both relationships are read")
    inspections = rels["WaterLines_Inspections"]
    check(inspections["origin"] == "WaterLines"
          and inspections["destination"] == "InspectionLog",
          "a relationship reads both class names")
    check(inspections["cardinality"] == "OneToMany",
          "the cardinality loses its esri prefix")
    check(inspections["composite"] is False, "a simple relationship is not composite")
    check(inspections["forward_label"] == "has inspections",
          "the forward path label is read")
    check(inspections["backward_label"] == "inspects",
          "the backward path label is read")
    check([k["field"] for k in inspections["origin_keys"]]
          == ["GLOBALID", "LINEID"],
          "both origin keys are read, primary first")
    check(inspections["origin_keys"][0]["role"] == "OriginPrimary",
          "each key carries its role")
    check(inspections["destination_keys"] == [],
          "an empty destination key list reads as empty")
    keyless = parse_item("R", "", RELATIONSHIP_XML.replace(
        "<DestinationClassKeys xsi:type='typens:ArrayOfRelationshipClassKey'>"
        "</DestinationClassKeys>", ""))
    check(keyless["destination_keys"] == [],
          "a relationship with NO destination key element reads as empty too")
    check(len(keyless["origin_keys"]) == 2,
          "and its origin keys are still read")
    unnamed = parse_item("WaterLines__ATTACH", "",
                         ATTACH_TABLE_XML.replace(
                             "<Name>WaterLines__ATTACH</Name>", "<Name></Name>"))
    check(unnamed["name"] == "WaterLines__ATTACH",
          "an item whose Definition carries an empty Name falls back to the "
          "GDB_Items name  <-- pinned defect")

    check(len(report["attachments"]) == 1,
          "exactly one of the two relationships is an attachment relationship")
    link = report["attachments"][0]
    check(link["class"] == "WaterLines" and
          link["attach_table"] == "WaterLines__ATTACH",
          "the attachment linkage names the class and its attachment table")
    check(link["origin_key"] == "GlobalID" and link["foreign_key"] == "REL_GLOBALID",
          "and both key fields, which ogrinfo does not put on one line")
    check(link["composite"] is True, "an attachment relationship is composite")
    check(link["attach_table_present"] is True,
          "the attachment table really is in this geodatabase")
    orphan = analyse([i for i in sample_items()
                      if i["name"] != "WaterLines__ATTACH"], "orphan.gdb")
    check(orphan["attachments"][0]["attach_table_present"] is False,
          "an attachment table missing from the .gdb is reported missing")
    check(attachment_links([inspections], set(["InspectionLog"])) == [],
          "an ordinary relationship is never read as attachment linkage")
    # Esri's suffix is exactly __ATTACH. A table somebody named __ATTACHMENTS
    # by hand CONTAINS that suffix without being one, and matching anywhere in
    # the name rather than at the end reports it as attachment linkage.
    lookalike = parse_item("R2", "", RELATIONSHIP_XML.replace(
        "<Name>InspectionLog</Name>", "<Name>WaterLines__ATTACHMENTS</Name>"))
    check(lookalike["destination"] == "WaterLines__ATTACHMENTS",
          "a destination table can hold the attachment suffix without ending "
          "in it")
    check(attachment_links([lookalike], set(["WaterLines__ATTACHMENTS"])) == [],
          "and it is not read as attachment linkage  <-- pinned defect")

    # ---- domains tied back to the datasets that use them
    check(domains["MaterialCD"]["used_by"] == ["WaterLines"],
          "GDB_ItemRelationships ties MaterialCD to WaterLines")
    check(domains["DiameterRG"]["used_by"] == ["WaterLines"],
          "and DiameterRG to the same feature class")
    check(domains["PressureFixed"]["used_by"] == [],
          "a domain no dataset uses is reported as used by nothing")
    check(analyse(sample_items(), "water.gdb")["domains"][0]["used_by"] == [],
          "with no relationship rows at all, no domain claims a user")
    noisy = analyse(sample_items(), "water.gdb", sample_relationship_rows() + [
        # The same link written twice, which a .gdb with both a field level and
        # a subtype level assignment really does carry.
        {"origin": UUID_WATERLINES, "dest": UUID_MATERIAL,
         "type": UUID_DOMAIN_IN_DATASET},
        # A row whose two ends are items this geodatabase does not have.
        {"origin": "{nothing}", "dest": "{nowhere}",
         "type": UUID_DOMAIN_IN_DATASET},
        # A row pointing at a real domain from an origin that is not a dataset.
        {"origin": "{nothing}", "dest": UUID_MATERIAL,
         "type": UUID_DOMAIN_IN_DATASET},
    ])
    noisy_domains = dict((d["name"], d) for d in noisy["domains"])
    check(noisy_domains["MaterialCD"]["used_by"] == ["WaterLines"],
          "a duplicated link names the dataset once, not twice")
    check(noisy_domains["PressureFixed"]["used_by"] == [],
          "a link whose ends are not in this geodatabase is ignored")
    # link_domains recomputes the lists, it does not add to what is there. A
    # second call with a different set of rows has to REMOVE the dataset the
    # first call recorded, or the report accumulates links no row supports.
    again = analyse(sample_items(), "water.gdb", sample_relationship_rows())
    link_domains(again["domains"], again["datasets"],
                 [{"origin": UUID_WATERLINES, "dest": UUID_PRESSURE,
                   "type": UUID_DOMAIN_IN_DATASET}])
    again_domains = dict((d["name"], d) for d in again["domains"])
    check(again_domains["PressureFixed"]["used_by"] == ["WaterLines"],
          "a second pass records the link its own rows carry")
    check(again_domains["MaterialCD"]["used_by"] == [],
          "and drops the one they do not, rather than accumulating"
          "  <-- pinned defect")

    # ---- what was not recognised
    check(len(report["unrecognised"]) == 1,
          "the workspace item is the only thing this fixture cannot read")
    check(report["unrecognised"][0]["root"] == "DEWorkspace",
          "and it is reported by its root element, not dropped")
    extra = analyse(sample_items() + [
        {"uuid": "{1}", "name": "Slope_Ned", "type": "{5ED667A3}",
         "definition": RASTER_XML},
        {"uuid": "{2}", "name": "", "type": "{F3783E6F}", "definition": ""},
    ], "water.gdb")
    unknown = dict((u["name"], u) for u in extra["unrecognised"])
    check(len(extra["unrecognised"]) == 3,
          "an unknown item type lands in UNRECOGNISED, it does not vanish"
          "  <-- pinned defect")
    check(unknown["Slope_Ned"]["root"] == "DERasterDataset",
          "a raster dataset is named with the root element gdbxray cannot read")
    check(unknown["Slope_Ned"]["type_uuid"] == "{5ED667A3}",
          "and keeps its item type uuid, which is what to look up next")
    check(unknown[""]["root"] == "",
          "an item with an EMPTY Definition is reported, not skipped")
    check(extra["counts"]["datasets"] == report["counts"]["datasets"],
          "and neither of them was counted as a dataset")

    # ---- the counts the report leads with
    counts = report["counts"]
    check(counts["items"] == 8, "the report counts every row it was given")
    check(counts["datasets"] == 2 and counts["domains"] == 3,
          "and splits them by what they turned out to be")
    check(counts["relationships"] == 2 and counts["unrecognised"] == 1,
          "including the ones it could not read")
    check(counts["subtypes"] == 3,
          "the subtype total is summed across datasets")
    check(counts["rules"] == 3, "so is the attribute rule total")
    check(counts["attachments"] == 1, "and the attachment linkage total")
    check(report["source"] == "water.gdb", "the report names where it came from")

    # ---- --item
    one = select(report, "WaterLines")
    check(len(one["datasets"]) == 1 and one["datasets"][0]["name"] == "WaterLines",
          "--item picks out one dataset")
    check(one["domains"] == [] and one["relationships"] == [],
          "and leaves the rest of the geodatabase out")
    check(len(one["attachments"]) == 1,
          "attachment linkage follows the CLASS, not the relationship name")
    check(one["counts"]["items"] == 1, "the counts are recomputed, not copied")
    check(one["counts"]["subtypes"] == 3 and one["counts"]["rules"] == 3,
          "the subtype and rule totals are recomputed too")
    check(select(report, "WaterLines__ATTACHREL")["attachments"][0]["class"]
          == "WaterLines",
          "asking for the relationship by name finds the same linkage")
    check(select(report, "MaterialCD")["counts"]["domains"] == 1,
          "--item works on a domain as well")
    # WaterLines is the only dataset in the fixture that HAS subtypes, so the
    # totals above are equally true of a copy of the whole report. The
    # attachment table has none, and its totals are where a copy shows up.
    bare = select(report, "WaterLines__ATTACH")
    check(bare["counts"]["datasets"] == 1,
          "--item picks out the attachment table as a dataset of its own")
    check(bare["counts"]["subtypes"] == 0 and bare["counts"]["rules"] == 0,
          "and reports ITS subtype and rule totals, not the whole "
          "geodatabase's  <-- pinned defect")
    check(select(report, "no-such-item")["counts"]["items"] == 0,
          "an item that is not there selects nothing")

    # ---- the printed report
    lines = render(redact_tree(report))
    text = "\n".join(lines)
    check(lines[0] == "gdbxray: water.gdb", "the report names the geodatabase")
    check("3 subtype(s) and 3 attribute rule(s)" in lines[2],
          "the summary leads with what ogrinfo does not print")
    check("FEATURE CLASS WaterLines" in text, "the feature class has a heading")
    check("subtype field: ASSETTYPE (default subtype 1)" in text,
          "the subtype field and default subtype are printed")
    check("subtype 2  Transmission main" in text,
          "each subtype is printed with its code and name")
    check("overrides the class default PVC" in text,
          "an overriding subtype default is called out with the value it beats")
    check("overrides the class domain MaterialCD" in text,
          "an overriding subtype domain is called out the same way")
    check("attribute rule 1 CalcCondition (calculation) on field CONDITION"
          in text, "each rule is printed with its type and target field")
    check("triggers: insert, update   evaluation order 1   enabled" in text,
          "with its triggers, order and enabled state")
    check("error 1001: Diameter must be greater than zero" in text,
          "a constraint rule prints its error number and message")
    check('| IIf($feature.DIAMETER >= 12, "MAIN", "LATERAL")' in text,
          "the Arcade expression is printed verbatim")
    check("AAPK6f2c9d1e4b7a3f" not in text,
          "and the token in the third rule is NOT printed  <-- pinned defect")
    check("token=" + MASK in text, "the masked parameter is visible as masked")
    check("WaterLines -> WaterLines__ATTACH  GlobalID -> REL_GLOBALID  composite"
          in text, "the attachment linkage is one line")
    check("PressureFixed" in text and "fixed at 150 (min equals max)" in text,
          "the single value range domain is printed as a fixed value")
    check("DiameterRG" in text and "2 to 48" in text,
          "an ordinary range prints both ends")
    check("MaterialCD" in text and "4 value(s)" in text,
          "a coded domain prints how many codes it holds")
    check("used by WaterLines" in text, "and which dataset uses it")
    check("UNUSED" in text, "a domain nothing uses is marked UNUSED")
    check("UNRECOGNISED (1)" in text and "DEWorkspace" in text,
          "the unrecognised item is printed by name and root element")
    check("ogrinfo prints these too" in text,
          "the sections ogrinfo already covers say so")
    check("WaterLines -> InspectionLog  OneToMany  GLOBALID -> LINEID" in text,
          "the ordinary relationship is printed with both ends and both keys")

    empty = render(analyse([], "empty.gdb"))
    check(len(empty) == 3,
          "an empty geodatabase renders the three summary lines and no sections")
    check("0 item(s)" in empty[1], "and says it found nothing")
    plain = render(analyse([{"uuid": "{1}", "name": "T", "type": "{2}",
                             "definition": ATTACH_TABLE_XML}], "plain.gdb"))
    check("TABLE" not in "\n".join(plain),
          "a table with no subtypes and no rules gets no section of its own")

    check(show_value(None) == "(none)",
          "a field with no default renders as (none)")
    check(show_value("") == "''",
          "a default that IS the empty string renders as quotes, so the two "
          "cannot be confused  <-- pinned defect")
    check(show_value("0") == "0", "a default of 0 renders as 0")

    # A feature class carrying rules and NO subtypes, which is what a class gets
    # when somebody adds a constraint rule and never uses subtypes at all. Built
    # by cutting the subtype block out of the real fixture, so the rest of the
    # document is still exactly what ArcGIS wrote.
    start = FEATURE_CLASS_XML.index("<Subtypes")
    end = FEATURE_CLASS_XML.index("</Subtypes>") + len("</Subtypes>")
    rules_only = (FEATURE_CLASS_XML[:start] + FEATURE_CLASS_XML[end:]
                  .replace("<Description>Set condition from diameter &amp; "
                           "size</Description>", "<Description></Description>"))
    lonely = render(analyse([{"uuid": "{1}", "name": "WaterLines",
                              "type": "{2}", "definition": rules_only}],
                            "rules.gdb"))
    lonely_text = "\n".join(lonely)
    check("FEATURE CLASS WaterLines" in lonely_text,
          "a class with rules and no subtypes still gets a section")
    check("subtype field:" not in lonely_text,
          "and no subtype heading above the rules it does have")
    check("attribute rule 1 CalcCondition" in lonely_text,
          "the rules are printed on their own")
    check("      \n" not in lonely_text
          and lonely_text.count("  attribute rule ") == 3,
          "a rule with an EMPTY description prints no blank description line")

    # ---- the json
    payload = json.loads(json.dumps(redact_tree(report), sort_keys=True))
    check(sorted(payload.keys()) == ["attachments", "counts", "datasets",
                                     "domains", "relationships", "source",
                                     "unrecognised"],
          "the json carries the seven top level keys and nothing else")
    check(payload["datasets"][0]["subtypes"][1]["fields"][0]["overrides_default"]
          is True, "the override flags survive the json round trip as booleans")
    check(payload["counts"]["rules"] == 3, "so do the counts, as ints")
    check("AAPK6f2c9d1e4b7a3f" not in json.dumps(payload),
          "and the token is not in the json either  <-- pinned defect")

    # ---- argument handling
    args = _parse(["water.gdb"])
    check(args.show_secrets is False, "--show-secrets defaults to OFF")
    check(args.apply is False, "--apply defaults to OFF")
    check(args.json is False, "--json defaults to OFF")
    check(args.out is None, "--out defaults to nothing being written")
    check(args.item is None, "--item defaults to the whole geodatabase")
    check(args.gdb == "water.gdb", "the geodatabase is a positional argument")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--self-test"]).gdb is None,
          "and needs no geodatabase beside it")
    check(_parse(["a.gdb", "--json"]).json is True, "--json is read")
    check(_parse(["a.gdb", "--item", "X"]).item == "X", "--item is read")
    check(_parse(["a.gdb", "--out", "o.json"]).out == "o.json", "--out is read")
    check(_parse(["a.gdb", "--apply"]).apply is True, "--apply is read")
    check(_parse(["a.gdb", "--show-secrets"]).show_secrets is True,
          "--show-secrets is read")

    # ---- printing a schema the console cannot encode
    # A Marion County geodatabase carries degree signs in descriptions and
    # accented names in coded values. On Linux stdout is UTF-8 and none of this
    # shows; on Windows a REDIRECTED stdout is the ANSI codepage, and print()
    # raises. These assertions run identically on both.
    narrow = NarrowStream()
    raises(lambda: narrow.write("\u4e2d\u6587"),
           "the narrow stream really does refuse a character cp1252 lacks, so "
           "the assertions below can fail", UnicodeEncodeError)
    wide = io.StringIO()
    emit("plain ascii", wide)
    check(wide.getvalue() == "plain ascii\n",
          "emit writes an ordinary line unchanged, with one newline")
    accented = NarrowStream()
    emit("R\u00e9sum\u00e9 90\u00b0", accented)
    check(accented.text.startswith("R\u00e9sum\u00e9 90\u00b0"),
          "a character the codepage DOES carry is written as itself, not escaped")
    hard = NarrowStream()
    emit("subtype 0  \u4e2d\u6587 pipe", hard)
    check("\\u4e2d" in hard.text,
          "a character it does NOT carry is escaped rather than thrown"
          "  <-- pinned defect")
    check("pipe" in hard.text and hard.text.startswith("subtype 0  "),
          "and the rest of the line survives around the escape")
    check(hard.text.endswith("\n"), "the line still ends")

    # The same thing end to end: main printing a report whose subtype name is
    # CJK, to a stream that cannot encode it, exits 0 rather than dying with a
    # traceback partway through the report.
    unicode_xml = ZERO_SUBTYPE_XML.replace(
        "<SubtypeName>Unknown</SubtypeName>",
        "<SubtypeName>\u4e2d\u6587 R\u00e9sum\u00e9</SubtypeName>")
    check("\u4e2d\u6587" in "\n".join(render(analyse(
        [{"uuid": "{1}", "name": "Poles", "type": "{2}",
          "definition": unicode_xml}], "u.gdb"))),
          "the rendered report really does carry the CJK subtype name")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass, which is the one failure no other
    # assertion here could see. Three deliberate failures are recorded against a
    # scratch mark and then taken back off the tally.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    check("no error raised" in probe[1] and "wrong exception" in probe[2],
          "and say which way the call under test went wrong")

    # ---- the OGR reader, against a stub that answers what read_gdb asks
    ogr = stub_ogr()
    items, rows = read_gdb("water.gdb", ogr)
    check(ogr.opened == ["water.gdb"], "read_gdb opens the path it was given")
    check(len(items) == 8, "it reads every row of GDB_Items")
    check(sorted(items[0].keys()) == ["definition", "name", "type", "uuid"],
          "each row carries the four columns this tool reads")
    check(items[0]["name"] == "WaterLines",
          "the Name column is read under its own key")
    check(items[0]["definition"].startswith("<DEFeatureClassInfo"),
          "and the Definition column with it")
    check(len(rows) == 2 and rows[0]["origin"] == UUID_WATERLINES,
          "GDB_ItemRelationships is read and its columns renamed")
    check(read_gdb("old.gdb", ogr)[1] == [],
          "a geodatabase with no GDB_ItemRelationships reads as no rows, not "
          "as a failure  <-- pinned defect")
    check(len(read_gdb("old.gdb", ogr)[0]) == 8,
          "and its items are still read in full")
    message = raises(lambda: read_gdb("notagdb.gdb", ogr),
                     "a source with no GDB_Items raises", IOError)
    check("GDB_Items" in message, "and the error names the table it wanted")
    message = raises(lambda: read_gdb("no-such.gdb", ogr),
                     "a path OGR cannot open raises", IOError)
    check("could not open" in message, "and the error says what failed")

    check(analyse(items, "water.gdb", rows)["counts"] == report["counts"],
          "the report built from the reader matches the one built from the "
          "fixtures, so the reader and the parser agree")

    gdal = import_ogr()
    check(gdal is None or hasattr(gdal, "Open"),
          "import_ogr returns the module or None, never a half built one")

    # ---- main, end to end, against real directories a real argument could name
    here = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="gdbxray-selftest-")
    try:
        os.chdir(tmp)
        disk_ogr = stub_ogr(tmp)
        water_gdb = os.path.join(tmp, "water.gdb")
        broken_gdb = os.path.join(tmp, "notagdb.gdb")
        stranger_gdb = os.path.join(tmp, "stranger.gdb")
        for made in (water_gdb, broken_gdb, stranger_gdb):
            os.mkdir(made)

        rc, out = capture(lambda: main([water_gdb], ogr=disk_ogr))
        check(rc == 0, "a successful read exits 0")
        check("FEATURE CLASS WaterLines" in out, "and prints the report")
        check("AAPK6f2c9d1e4b7a3f" not in out,
              "with no token on stdout  <-- pinned defect")
        rc, out = capture(lambda: main([water_gdb, "--show-secrets"],
                                       ogr=disk_ogr))
        check(rc == 0 and "AAPK6f2c9d1e4b7a3f" in out,
              "--show-secrets prints the token, so the masking above is real")
        # main printing a report the console cannot encode. Before emit() this
        # died with a UnicodeEncodeError traceback and exit 1, halfway through
        # the report, on Windows only.
        unicode_gdb = os.path.join(tmp, "unicode.gdb")
        os.mkdir(unicode_gdb)
        unicode_ogr = StubOgr({unicode_gdb: StubSource(
            {ITEMS_TABLE: [{"UUID": "{1}", "Name": "Poles", "Type": "{2}",
                            "Definition": unicode_xml}]}, "unicode.gdb")})
        narrow_out = NarrowStream()
        real_stdout = sys.stdout
        sys.stdout = narrow_out
        try:
            rc = main([unicode_gdb], ogr=unicode_ogr)
        finally:
            sys.stdout = real_stdout
        check(rc == 0,
              "a report the console cannot encode still exits 0, not a "
              "traceback  <-- pinned defect")
        check("\\u4e2d\\u6587" in narrow_out.text,
              "the name it could not encode is escaped into the report")
        check("R\u00e9sum\u00e9" in narrow_out.text,
              "the part of the same name the codepage CAN carry is intact")
        check("subtype 0" in narrow_out.text and "POLETYPE" in narrow_out.text,
              "and the rest of the report is printed as usual")

        rc, out = capture(lambda: main([water_gdb, "--json"], ogr=disk_ogr))
        check(rc == 0 and json.loads(out)["counts"]["subtypes"] == 3,
              "--json prints parseable json and nothing else")
        check("AAPK6f2c9d1e4b7a3f" not in out, "which is masked as well")
        rc, out = capture(lambda: main([water_gdb, "--item", "MaterialCD"],
                                       ogr=disk_ogr))
        check(rc == 0 and "MaterialCD" in out and "FEATURE CLASS" not in out,
              "--item narrows the printed report to one item")
        rc, out = capture(lambda: main([water_gdb, "--item", "nope"],
                                       ogr=disk_ogr))
        check(rc == 64,
              "--item naming nothing is a usage error, not an empty report"
              "  <-- pinned defect")
        check("no item named" in out, "and says which name found nothing")
        rc, out = capture(lambda: main([], ogr=disk_ogr))
        check(rc == 64, "no geodatabase and no --self-test is a usage error")
        check("--self-test" in out,
              "and the message names the flag that needs none")
        rc, out = capture(lambda: main([broken_gdb], ogr=disk_ogr))
        check(rc == 2, "a geodatabase that cannot be read exits 2")
        check("GDB_Items" in out, "and the reason reaches the operator")

        # ---- writing a file, the only thing that touches the disk
        target = os.path.join(tmp, "schema.json")
        rc, out = capture(lambda: main([water_gdb, "--json", "--out", target],
                                       ogr=disk_ogr))
        check(rc == 0 and not os.path.exists(target),
              "--out without --apply writes NOTHING  <-- pinned defect")
        check("Re-run with --apply" in out,
              "it names the file it would write and how to write it")
        rc, out = capture(lambda: main([water_gdb, "--json", "--out", target,
                                        "--apply"], ogr=disk_ogr))
        check(rc == 0 and os.path.isfile(target),
              "--out --apply writes the file")
        written = io.open(target, encoding="utf-8").read()
        check(json.loads(written)["counts"]["rules"] == 3,
              "the file on disk is the json report")
        check("AAPK6f2c9d1e4b7a3f" not in written,
              "and the token is not in the file either  <-- pinned defect")
        check("token=" + MASK in written,
              "the parameter is there, masked, so the check above can fail")
        rc, out = capture(lambda: main([water_gdb, "--json", "--out", target,
                                        "--apply"], ogr=disk_ogr))
        check(rc == 2 and io.open(target, encoding="utf-8").read() == written,
              "a second --apply refuses to overwrite and changes nothing")
        check("Refusing to overwrite" in out, "and names the file it left alone")
        plain_target = os.path.join(tmp, "schema.txt")
        rc, out = capture(lambda: main([water_gdb, "--out", plain_target,
                                        "--apply"], ogr=disk_ogr))
        check(rc == 0 and "FEATURE CLASS WaterLines"
              in io.open(plain_target, encoding="utf-8").read(),
              "--out without --json writes the printed report")
        bad = os.path.join(tmp, "no-such-directory", "schema.json")
        rc, out = capture(lambda: main([water_gdb, "--out", bad, "--apply"],
                                       ogr=disk_ogr))
        check(rc == 2, "a file that cannot be written exits 2, not a traceback")
        check("could not write" in out, "and names the file it could not write")

        # A geodatabase argument is checked against the filesystem before OGR is
        # asked for anything, so a typo is a usage error rather than a driver
        # error somebody has to read twice.
        rc, out = capture(lambda: main(["no-such-path-8f31a2.gdb"],
                                       ogr=disk_ogr))
        check(rc == 64, "a path that does not exist is a usage error"
                        "  <-- pinned defect")
        check("no such" in out.lower(), "and the message says so")
        rc, out = capture(lambda: main([stranger_gdb], ogr=disk_ogr))
        check(rc == 2 and "could not open" in out,
              "a directory that EXISTS but is not a geodatabase is a read "
              "failure, not a usage error")

        # The same call with no ogr handed to it, so main does the lazy import
        # itself. What it then says depends on whether this interpreter has
        # GDAL, and both answers are checked rather than only the local one.
        expected = ("could not open" if import_ogr() is not None
                    else "GDAL/OGR is not importable")
        rc, out = capture(lambda: main([stranger_gdb]))
        check(rc == 2,
              "main importing GDAL for itself still exits 2 on that directory")
        check(expected in out,
              "and says either that GDAL is missing or that OGR refused the "
              "path, whichever is true of this interpreter")
    finally:
        os.chdir(here)
        shutil.rmtree(tmp, ignore_errors=True)
    check(not os.path.isdir(tmp),
          "the self-test leaves no temporary directory behind  <-- pinned defect")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def emit(text, stream=None):
    """Print one block of text, whatever the stream can encode.

    Windows hands a REDIRECTED stdout the ANSI codepage rather than UTF-8, so
    an accented subtype name, a degree sign in a domain description or a
    non-ASCII string literal inside an Arcade expression makes print() raise
    UnicodeEncodeError, and the tool dies with a traceback instead of its exit
    code. Linux defaults to UTF-8 and never shows this, which is exactly why it
    has to be handled here rather than noticed later.

    The characters the stream cannot carry are escaped rather than dropped, so
    the name stays identifiable and the report still exits 0. --json is not
    affected, because json.dumps escapes non-ASCII on the way out already.
    """
    stream = sys.stdout if stream is None else stream
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, "backslashreplace").decode(encoding),
              file=stream)


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="gdbxray.py",
        description="Print the File Geodatabase schema ogrinfo will not show "
                    "you: subtypes, attribute rules and attachment linkage.",
        epilog="Nothing is written without --apply, and credentials found in "
               "an Arcade expression are masked unless --show-secrets is given.",
    )
    ap.add_argument("gdb", nargs="?",
                    help="the .gdb DIRECTORY to read")
    ap.add_argument("--item", metavar="NAME",
                    help="report only the item with this name")
    ap.add_argument("--json", action="store_true",
                    help="emit the report as json instead of text")
    ap.add_argument("--out", metavar="FILE",
                    help="write the report to FILE. Needs --apply to write.")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without this nothing is "
                         "written, and an existing file is never overwritten.")
    ap.add_argument("--show-secrets", dest="show_secrets", action="store_true",
                    help="print tokens and passwords found in Arcade "
                         "expressions instead of masking them. OFF by default.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit. Needs no GDAL "
                         "and no geodatabase.")
    return ap.parse_args(argv)


def main(argv=None, ogr=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.gdb:
        emit("error: name a .gdb to read. Use --self-test to verify the tool "
             "without a geodatabase.", sys.stderr)
        return 64
    if not os.path.exists(args.gdb):
        # A typo here would otherwise come back as a driver error, which reads
        # like a corrupt geodatabase rather than a wrong path.
        emit("error: no such file or directory: %s" % args.gdb, sys.stderr)
        return 64

    if ogr is None:
        ogr = import_ogr()
        if ogr is None:
            emit("error: GDAL/OGR is not importable from this interpreter, so "
                 "a real geodatabase cannot be read. Run this with ArcGIS "
                 "Pro's Python, or a python3 with the GDAL bindings "
                 "installed. --self-test needs neither.", sys.stderr)
            return 2

    try:
        items, rows = read_gdb(args.gdb, ogr)
    except Exception as exc:
        emit("error: %s" % exc, sys.stderr)
        return 2

    report = analyse(items, args.gdb, rows)
    if args.item:
        report = select(report, args.item)
        if not report["counts"]["items"]:
            emit("error: no item named %s in %s" % (args.item, args.gdb),
                 sys.stderr)
            return 64
    if not args.show_secrets:
        report = redact_tree(report)

    if args.json:
        text = json.dumps(report, indent=2, sort_keys=True)
    else:
        text = "\n".join(render(report))

    if not args.out:
        emit(text)
        return 0

    if not args.apply:
        emit("Would write %d character(s) to %s. Re-run with --apply to write "
             "it." % (len(text), args.out))
        return 0
    if os.path.exists(args.out):
        emit("error: %s already exists. Refusing to overwrite it."
             % args.out, sys.stderr)
        return 2
    try:
        with io.open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except (OSError, IOError) as exc:
        emit("error: could not write %s: %s" % (args.out, exc), sys.stderr)
        return 2
    emit("Wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
