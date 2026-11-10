"""Excel template generated from the canonical schema: one sheet per table with its columns (required ones marked),
drop-down lists for columns with allowed values, and a "Fields" sheet that describes every column. A customer fills
it in and loads it with DATA_SOURCE=excel – no mapping file needed."""
from __future__ import annotations

from pathlib import Path

from app.data_adapters.schema import SCHEMA, describe

FIELDS_SHEET = "Fields"
MAX_ROWS = 2000  # rows covered by the drop-down lists


def write_template(path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    path = Path(path)
    wb = Workbook()
    wb.remove(wb.active)
    required_fill = PatternFill("solid", fgColor="FFF2CC")
    fields = wb.create_sheet(FIELDS_SHEET)
    fields.append(["Table", "Column", "Type", "Required", "Key", "Description"])
    for cell in fields[1]:
        cell.font = Font(bold=True)
    for spec in SCHEMA.values():
        ws = wb.create_sheet(spec.name)
        columns = list(spec.columns) + [c for c in spec.required if c not in spec.columns]
        ws.append(columns)
        for idx, column in enumerate(columns, start=1):
            cell = ws.cell(row=1, column=idx)
            cell.font = Font(bold=True)
            if column in spec.required:
                cell.fill = required_fill
            ws.column_dimensions[cell.column_letter].width = max(12, len(column) + 2)
            if column in spec.allowed:
                validation = DataValidation(type="list", formula1='"' + ",".join(spec.allowed[column]) + '"',
                                            allow_blank=True, showErrorMessage=True,
                                            error=f"Allowed: {', '.join(spec.allowed[column])}")
                ws.add_data_validation(validation)
                validation.add(f"{cell.column_letter}2:{cell.column_letter}{MAX_ROWS}")
            fields.append([spec.name, column, spec.column_type(column), "yes" if column in spec.required else "",
                           "yes" if column == spec.key else "", describe(spec.name, column)])
        ws.freeze_panes = "A2"
    for letter, width in zip("ABCDEF", (18, 30, 8, 9, 5, 70), strict=True):
        fields.column_dimensions[letter].width = width
    wb.move_sheet(FIELDS_SHEET, offset=len(wb.sheetnames))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
