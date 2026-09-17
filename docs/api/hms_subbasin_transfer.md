# HmsSubbasinTransfer

Deterministic compilation of HMS-subbasin to RAS precipitation-grid transfer
maps for `hms-subbasin-volume-conserving-v1`.

The compiler consumes an authenticated RAS Commander precipitation application
area, explicit HMS source areas and units, exact selected subbasin polygons,
and portable HMS model identities. It attributes target cells by center-point
containment and divides each source area by its assigned effective RAS
receiving area. Runtime DSS reading, time alignment, grid publication, basin
qualification, and engineering approval are separate workflow stages.

::: hms_commander.HmsSubbasinTransfer
    options:
      show_source: false
      heading_level: 2
