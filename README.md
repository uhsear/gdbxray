# gdbxray

Print the File Geodatabase schema ogrinfo will not show you: subtypes, attribute rules and attachment linkage.

## Start with ogrinfo, because two thirds of this is already solved

GDAL's OpenFileGDB driver reads coded and range domains, with their codes, descriptions, field
types and split and merge policies, and since GDAL 3.6 it reads relationship classes as well.

```
ogrinfo -json water.gdb
```

That prints every domain in the geodatabase, correctly, and every relationship class with its
cardinality and key fields, and it needs no licence and no Esri install. Nothing here replaces
it. If domains and relationships are what you are after, stop reading and use `ogrinfo`.

Three parts of the schema are still not in that output, and they are the parts that do not
survive a move to any other format.

- **Subtypes.** Run `ogrinfo -json` over the feature class in the worked example below, which has
  three subtypes, and the word "subtype" appears three times: twice inside a coordinate system
  description and once as the OGR field subType `Int16`. The three subtype names, their codes,
  the default subtype, and the per-subtype field defaults and domains are not in the output at
  all. That is not a GDAL defect; OGR has no subtype concept to map them onto.
- **Attribute rules.** The Arcade expression text, the rule type, which of insert, update and
  delete fire it, the evaluation order, the error number and message. None of it is reported.
- **Attachment linkage.** This is the weakest of the three, and the README says so. `ogrinfo`
  does report the `__ATTACHREL` relationship and marks its `related_table_type` as `media`, so
  the linkage is not invisible. What it does not do is put the attachment table, the class it
  hangs off and both key fields on one line, or tell you whether that attachment table is still
  in the geodatabase.

A fourth thing falls out of reading the two system tables: a domain that no dataset uses. That is
dead weight a catalog pane will show you happily for years.

```
$ python gdbxray.py --self-test
gdbxray self-test: no GDAL, no geodatabase, no network
--------------------------------------------------------------------
PASS  esriARTEInsert reads as Insert, not as EInsert  <-- pinned defect
PASS  MaterialCD has EXACTLY four coded values
PASS  a CDATA description with a bare ampersand parses  <-- pinned defect
PASS  and the CDATA markers are not part of the text  <-- pinned defect
PASS  a range whose min EQUALS its max is reported, not skipped as empty  <-- pinned defect
PASS  two MISSING ends are not a fixed range  <-- pinned defect
PASS  a subtype default that DIFFERS from the class default is flagged  <-- pinned defect
PASS  a subtype whose code is 0 keeps the code 0  <-- pinned defect
PASS  a DEFAULT subtype code of 0 survives too  <-- pinned defect
PASS  a field default of 0 is a default, not an absent one  <-- pinned defect
PASS  the delete trigger flag is NOT set for a rule that omits it  <-- pinned defect
PASS  the token is gone from the masked expression  <-- pinned defect
PASS  an unknown item type lands in UNRECOGNISED, it does not vanish  <-- pinned defect
PASS  and the token in the third rule is NOT printed  <-- pinned defect
PASS  and the token is not in the json either  <-- pinned defect
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  a geodatabase with no GDB_ItemRelationships reads as no rows, not as a failure  <-- pinned defect
PASS  --out without --apply writes NOTHING  <-- pinned defect
PASS  and the token is not in the file either  <-- pinned defect
PASS  a path that does not exist is a usage error  <-- pinned defect
PASS  the self-test leaves no temporary directory behind  <-- pinned defect
--------------------------------------------------------------------
300 assertions, 0 failed
```

Those twenty-one lines are a selection. The run prints 300.

## Requirements

Python 3.9 or newer. The parsing is standard library only: `xml.etree.ElementTree`, `json`,
`argparse`, `re`. Nothing to install.

Reading a real `.gdb` needs GDAL/OGR, and only that. The import is lazy and guarded, so
`--self-test` runs to completion on a machine that has never had GDAL on it, and a real read
without GDAL is a clear message rather than an ImportError traceback.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" gdbxray.py water.gdb
python3 gdbxray.py water.gdb      # any python3 with the GDAL bindings
python3 gdbxray.py --self-test    # no GDAL needed
```

```
git clone https://github.com/uhsear/gdbxray.git
```

## Quick start

```
python gdbxray.py --self-test
python gdbxray.py water.gdb
```

## Usage

```
python gdbxray.py water.gdb
python gdbxray.py water.gdb --json
python gdbxray.py water.gdb --item WaterLines
python gdbxray.py water.gdb --json --out schema.json --apply
```

| Flag | Default | What it does |
|---|---|---|
| `GDB` | none | The `.gdb` directory to read. A path that does not exist is a usage error, not an empty report. |
| `--item` | none | Report only the item with this name. A name nothing matches is a usage error. |
| `--json` | off | Emit the report as JSON instead of text. |
| `--out` | none | Write the report to a file. Needs `--apply` to write. |
| `--apply` | off | Write the `--out` file. Without it nothing is written, and an existing file is never overwritten. |
| `--show-secrets` | off | Print credentials found in an Arcade expression instead of masking them. |
| `--self-test` | off | Run the assertions and exit. Needs no GDAL and no geodatabase. |

Exit codes: 0 read, 2 a read or write step failed, 64 usage error.

## What it prints

This is a real run against a File Geodatabase built with `arcpy`: one feature class with three
subtypes, three attribute rules, attachments, a related table and four domains.

```
$ python gdbxray.py water.gdb
gdbxray: water.gdb
11 item(s): 3 dataset(s), 4 domain(s), 2 relationship(s), 2 unrecognised
3 subtype(s) and 3 attribute rule(s), which ogrinfo does not print

FEATURE CLASS WaterLines
  subtype field: ASSETTYPE (default subtype 1)
  subtype 1  Distribution main
      MATERIAL         default PVC        domain MaterialCD
      DIAMETER         default 8          domain DiameterRG
  subtype 2  Transmission main
      MATERIAL         default DI         domain MaterialCD   <-- overrides the class default PVC
      DIAMETER         default 24         domain DiameterRG   <-- overrides the class default 8
  subtype 3  Hydrant lateral
      DIAMETER         default 8          domain DiameterRG
      MATERIAL         default DI         domain HydrantMaterialCD   <-- overrides the class default PVC   <-- overrides the class domain MaterialCD
  attribute rule 1 CalcCondition (calculation) on field CONDITION
      triggers: insert, update   evaluation order 1   enabled
      Set condition from diameter & size
      | IIf($feature.DIAMETER >= 12, "MAIN", "LATERAL")
  attribute rule 2 DiameterPositive (constraint)
      triggers: insert, update   evaluation order 1   enabled
      Diameter must be > 0 <always>
      error 1001: Diameter must be greater than zero
      | $feature.DIAMETER > 0
  attribute rule 3 StampDiameter (calculation) on field DIAMETER
      triggers: insert   evaluation order 2   enabled
      Copies the diameter; the service url still carries token=[redacted]
      | var svc = "https://services1.arcgis.com/ExAmPlE/arcgis/rest/services/Assets/FeatureServer/0/query?where=1=1&token=[redacted]&f=json"; return $feature.DIAMETER

ATTACHMENTS
  WaterLines -> WaterLines__ATTACH  GlobalID -> REL_GLOBALID  composite

DOMAINS (ogrinfo prints these too)
  MaterialCD           coded  String    4 value(s)                   used by WaterLines
  DiameterRG           range  Integer   2 to 48                      used by WaterLines
  PressureFixed        range  Integer   fixed at 150 (min equals max) UNUSED
  HydrantMaterialCD    coded  String    3 value(s)                   used by WaterLines

RELATIONSHIPS (ogrinfo prints these too)
  WaterLines__ATTACHREL    WaterLines -> WaterLines__ATTACH  OneToMany  GlobalID -> REL_GLOBALID
  WaterLines_Inspections   WaterLines -> InspectionLog  OneToMany  GLOBALID -> LINEID

UNRECOGNISED (2)
  (unnamed)                (empty definition)
  Workspace                DEWorkspace
```

Every line above the `DOMAINS` heading is absent from `ogrinfo -json` over the same geodatabase.

## Credentials in an Arcade expression

An attribute rule is source code that lives inside a geodatabase, and people hardcode service
tokens in it. A schema dump is exactly the artefact that then gets pasted into a ticket.

Anything that looks like `token=`, `password=`, `pwd=`, `apikey=`, `client_secret=` or an
`Authorization: Bearer` header is masked before the report is printed or written. The masking
runs over every string in the report, not a list of interesting fields, so a token pasted into a
domain description is masked by the same three lines.

`--show-secrets` turns it off, and is OFF by default.

## How it works

`GDB_Items` holds one row per item in the geodatabase, and the `Definition` column of that row is
the item's schema as XML. `GDB_ItemRelationships` holds the links between items by UUID.
OpenFileGDB hides both tables from the layer list, and still hands either over by name, so no
open option and no particular GDAL build is needed.

gdbxray dispatches on the ROOT ELEMENT of each `Definition` rather than on the item type GUID,
which keeps a table of thirty GUIDs out of the file and reads the answer the XML itself gives. An
item whose root element is none of the five it handles is listed under `UNRECOGNISED` with its
name and its type GUID. A geodatabase full of rasters reports sixteen unrecognised items and
says so, rather than reporting nothing and looking clean.

## What it will not do

- **It does not replace `ogrinfo`.** Domains and relationships are printed here because the
  subtype report is unreadable without them, and both sections say so in their heading.
- **No geometry, no extents, no spatial reference, no row counts.** `ogrinfo -al -so` does all of
  that better.
- **No Utility Network, no parcel fabric, no topology, no geometric network.** Those are
  extension datasets with their own Definition schemas, and they are reported as unrecognised
  rather than half read.
- **No contingent values.** They live in the `ContingentValues` column of `GDB_Items`, not in
  `Definition`, and they are a separate job.
- **No writing back.** The `--out` file is a report. Nothing here edits a geodatabase, and
  `--apply` only authorises writing that one file.
- **No mobile geodatabase and no enterprise geodatabase.** File Geodatabase only.
- **Characters the console cannot encode are escaped, not dropped.** Windows gives a
  redirected stdout the ANSI codepage rather than UTF-8, so an accented subtype name or a
  degree sign in a domain description makes a plain `print` raise, and the tool would die
  with a traceback rather than its exit code. Those characters are written as `\uXXXX`
  escapes instead and the report still exits 0. `--out --apply` writes UTF-8 and is never
  affected, and neither is `--json`, which escapes non-ASCII on the way out already.
- **A DOCTYPE in a Definition is refused rather than parsed.** ArcGIS never writes one. The
  standard library parser resolves no external entity, so a `file:///` reference reads nothing,
  but it does expand internal ones, and refusing the declaration closes that without growing a
  dependency to read a local file the operator already owns.

## Verification

`--self-test` runs 300 assertions. It needs no GDAL, no geodatabase, no network and no database,
and it produces the same 300 on Windows and on Linux.

The parsers work from real `Definition` XML copied out of a geodatabase built with `arcpy`, with
the spatial reference and extent blocks cut out. The OGR reader is driven through a stub that
answers the four calls it makes, and then run for real against a File Geodatabase that carries
three subtypes, three attribute rules, attachments and an unused domain. Branch coverage is 99
percent, and the suite is mutation tested: of 80 mutants planted in the parsers, the
report, the masking and the CLI, 78 were caught. The two survivors are equivalent
mutants, not gaps. Four places are not covered offline: the two lines where the GDAL import succeeds and
the branch in `main` that follows them, both of which need GDAL in the interpreter doing the
measuring; the self-test's own failure report, which runs only when an assertion fails; and the
`if __name__` guard, whose other arm is taken only when the file is imported.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.
