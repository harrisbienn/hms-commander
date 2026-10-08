# HmsSubbasinTransfer

Deterministic compilation of HMS-subbasin to RAS precipitation-grid transfer
maps for `hms-subbasin-volume-conserving-v1`.

The compiler consumes an authenticated RAS Commander precipitation application
area, explicit HMS source areas and units, exact selected subbasin polygons,
and portable HMS model identities. It attributes target cells by center-point
containment and divides each source area by its assigned effective RAS
receiving area. Runtime DSS reading, time alignment, grid publication, basin
qualification, and engineering approval are separate workflow stages. The
runtime API performs the first three with exact interval-end semantics and
isolated write/readback verification. Source selection treats dated D-part
records as one logical DSS family and supports the blank A-part used by HMS
output. Qualification and approval remain with the consuming study.

## Strict center selection and full-cell scaling

Pass `method="hms-subbasin-centroid-full-cell-v1"` to `compile_transfer_map`
to use the September 29 engineering-prototype rule. Supply a RAS
`ras-mesh-center-selected-full-cell-area` application-area 2.0 artifact and
the explicit list of internal subbasins selected by the study. Target centers
must be strictly inside a RAS mesh polygon and a selected subbasin; centers
on boundaries receive zero. There is no nearest-cell fill.

This method derives source areas from the selected subbasin polygons after
reprojection into the target meter CRS. It divides each area by the sum of
the **full areas** of its assigned target grid cells. Explicit source-area
columns remain required only by the original method. Empty receiving support
and ambiguous or overlapping polygons fail closed.

Maps, audits, and excess-product manifests use version 2.0 and record
`area_basis="selected-full-grid-cells"`. Volume checks apply to that grid
allocation; they do not certify the volume received inside the hydraulic
mesh. Keep the RAS application's separate mesh-intersection evidence for
engineering review. The original method and its 1.0 artifacts are unchanged.

HMS scenario-worker request **1.1** adds this method with the same
`spatial_transfer` fields used by the original subbasin method. Request 1.0
rejects the new method. The result envelope remains 1.0 and reports the
versioned product manifest. Both methods remain qualification-only and
forecast-ineligible. DSS publication retains bounded interval-end `PER-CUM`
semantics, explicit grid metadata, and native readback; it does not reproduce
the prototype's timestamp shift or implicit grid-origin handling.

::: hms_commander.HmsSubbasinTransfer
    options:
      show_source: false
      heading_level: 2

## Multiple receiving areas

A center-selected RAS application-area 3.0 produces transfer-map 3.0 with explicit `model.two_d_flow_areas`. Selection and full-cell scaling are unchanged. Areas share one attribution grid and one denominator per subbasin; do not sum independently scaled maps. Single-area inputs retain transfer-map 2.0. Audit/product 2.0 and worker request 1.1 remain unchanged because they authenticate the map by hash.

## Explicit delivered publication conventions

Use `apply_delivered_transfer_map_to_dss()` when reproducing a supplied workflow
that allocates on the map's original centroid grid, floors the DSS origin to
whole-cell coordinates, and stamps each excess interval-end value as the output
interval start. The arguments match `apply_transfer_map_to_dss()`; the map must
use the centroid/full-cell allocation method. Recompile the map against the
intended current model before use.

The new product method is `hms-subbasin-centroid-delivered-v1`. Product and audit
3.0 retain both allocation and published origins, source model window, label
offset and actual published start/end. A source window 00:00–01:00 with five-minute
excess produces grid coverage 00:05–01:05. This behavior is intentional for
reproduction and must not be reported as complete 00:00–01:00 coverage.

The existing allocation-map identity, exact source-series validation, overwrite
refusal, isolated native writer, readback and volume checks are reused. Existing
corrected/default APIs and worker request 1.1 are unchanged. This new API is a
separate preparation boundary; normalized worker dispatch and study acceptance
require an explicit consuming integration. No model is automatically switched
and no engineering or forecast approval is granted by a successful publication.
