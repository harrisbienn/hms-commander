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

::: hms_commander.HmsSubbasinTransfer
    options:
      show_source: false
      heading_level: 2
