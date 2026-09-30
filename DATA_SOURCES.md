# Data sources, access, and attribution

## Copernicus DEM GLO-30

This repository downloads Copernicus DEM GLO-30 data from the **Copernicus Data Space Ecosystem (CDSE)**. No Copernicus DEM raster data are included in this repository.

Official collection page:

https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM

Official Copernicus Contributing Missions documentation:

https://documentation.dataspace.copernicus.eu/Data/Others/CCM.html

The downloader targets the `2024_1` release. CDSE documents that release as the July 2024 GLO-30 release.

## Supported GLO-30 product profiles

The script exposes a true/false selector for the two documented GLO-30 product profiles:

- `COP-DEM_GLO-30-DGED/2024_1` — DGED profile, GeoTIFF DEM, 1 arc-second grid, 32-bit floating point.
- `COP-DEM_GLO-30-DTED/2024_1` — DTED profile, DTED Level 2 DEM, 1 arc-second grid, 16-bit signed.

The Copernicus DEM Product Handbook documents the DGED DEM naming pattern as `*_DEM.tif` and the 1 arc-second DTED DEM naming pattern as `*_DEM.dt2`.

Product Handbook:

https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf

## CCM authorization requirement

Protected GLO-30 download access requires a CDSE account registered/authorized for **Copernicus Contributing Missions (CCM)** data. CDSE currently instructs users who need Copernicus DEM 30 m access to enable the CCM-interest option in their CDSE identity profile and complete any authorization steps applicable to their account category.

Relevant CDSE announcement:

https://dataspace.copernicus.eu/news/2026-8-25-copernicus-dem-30m-view-service-update

The downloader cannot create, approve, or bypass CCM authorization. Persistent HTTP 403 responses are treated as access failures rather than retried indefinitely.

## APIs used

Catalogue:

https://catalogue.dataspace.copernicus.eu/odata/v1/Products

Protected product download service:

https://download.dataspace.copernicus.eu/odata/v1

CDSE identity/token service:

https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token

CDSE bulk-download guidance documents OData as an official programmatic method for filtered downloads and S3-compatible storage as another supported bulk-access route.

## Copernicus DEM licensing is separate from this repository's MIT license

The MIT License in this repository applies only to the **software and repository content authored for this project**. It does not relicense Copernicus DEM data.

Review the current official Copernicus DEM license and user obligations before redistribution or publication. CDSE currently requests the following source notice when communicating/distributing GLO-30 to the general public:

> © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved

For research use, CDSE lists the Copernicus DEM DOI:

https://doi.org/10.5270/ESA-c5d3d65

Always check the current provider terms rather than relying only on this repository's summary.

## Country boundaries

Country-mode selection uses ADM0 geometries from the geoBoundaries `gbOpen` API unless the user supplies a local GeoJSON override.

https://www.geoboundaries.org/

Boundary geometries are operational inputs for raster selection. They should not be interpreted as a statement by this repository about sovereignty or boundary recognition. Users should verify that the chosen boundary source is appropriate for their research.
