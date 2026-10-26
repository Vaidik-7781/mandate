import {
  CellStyleModule,
  ClientSideRowModelModule,
  ModuleRegistry,
  NumberFilterModule,
  PaginationModule,
  QuickFilterModule,
  TextFilterModule,
  themeQuartz,
} from "ag-grid-community";
import type { ColDef } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useMemo, useState } from "react";

// Only the modules the console uses, not the whole community bundle.
ModuleRegistry.registerModules([
  ClientSideRowModelModule,
  PaginationModule,
  TextFilterModule,
  NumberFilterModule,
  QuickFilterModule,
  CellStyleModule,
]);

// Mandate's own tokens drive the grid, so it follows light and dark mode with the rest of the page.
const theme = themeQuartz.withParams({
  backgroundColor: "var(--card)",
  foregroundColor: "var(--ink)",
  borderColor: "var(--hairline)",
  headerBackgroundColor: "var(--sunken)",
  headerTextColor: "var(--ink-2)",
  oddRowBackgroundColor: "transparent",
  rowHoverColor: "var(--seal-wash)",
  accentColor: "var(--seal)",
  fontFamily: "var(--font-ui)",
  fontSize: 14,
  headerFontWeight: 600,
  wrapperBorderRadius: 14,
  inputFocusBorder: { color: "var(--focus)" },
});

export type Col<T> = ColDef<T>;

/** AG Grid Community wrapped for the console: sortable, filterable, paged, keyboard and screen-reader friendly. */
export default function DataGrid<T extends object>({
  rows,
  columns,
  label,
  empty,
  pageSize = 10,
}: {
  rows: T[];
  columns: ColDef<T>[];
  label: string;
  empty: string;
  pageSize?: number;
}) {
  const [quick, setQuick] = useState("");
  const defaultColDef = useMemo<ColDef<T>>(
    () => ({ sortable: true, filter: true, resizable: true, flex: 1, minWidth: 110, wrapText: false }),
    [],
  );
  return (
    <div className="stack-sm">
      <label className="field" style={{ maxWidth: 320 }}>
        <span className="sr-only">Filter {label}</span>
        <input type="text" value={quick} placeholder={`Filter ${label}`} onChange={(e) => setQuick(e.target.value)} />
      </label>
      <div className="grid-wrap" role="region" aria-label={label}>
        <AgGridReact<T>
          theme={theme}
          rowData={rows}
          columnDefs={columns}
          defaultColDef={defaultColDef}
          quickFilterText={quick}
          pagination
          paginationPageSize={pageSize}
          paginationPageSizeSelector={false}
          domLayout="autoHeight"
          suppressCellFocus={false}
          overlayNoRowsTemplate={`<span class="muted">${empty}</span>`}
        />
      </div>
    </div>
  );
}
