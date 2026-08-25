\# data/raster/



This folder holds the per-country Global Horizontal Irradiance (GHI) rasters used by the pipeline. \*\*Not version-controlled\*\* (see `.gitignore`) - download each file yourself under the terms below before running the pipeline.



\## Source



All six rasters come from the same provider: \*\*Global Solar Atlas 3.0\*\* (World Bank Group / Solargis), Global Horizontal Irradiation, \~250 m resolution, 1999–2018 daily average, released under \*\*CC BY 4.0\*\*.



\## Download links and expected filenames



| Country | Download page | Expected filename |

|---|---|---|

| Haïti | https://globalsolaratlas.info/download/haiti | `GHI\_Haiti.tif` |

| La Réunion | https://globalsolaratlas.info/download/reunion | `GHI\_Reunion.tif` |

| Madagascar | https://globalsolaratlas.info/download/madagascar | `GHI\_Madagascar.tif` |

| Maroc | https://globalsolaratlas.info/download/morocco | `GHI\_Maroc.tif` |

| Kenya | https://globalsolaratlas.info/download/kenya | `GHI\_Kenya.tif` |

| Sénégal | https://globalsolaratlas.info/download/senegal | `GHI\_Senegal.tif` |



Place each downloaded GeoTIFF directly in this folder using the exact filename listed above - this must match `raster\_filename` in the corresponding `config/<country>.json`.



\## Required citation (per CC BY 4.0)



> Source: Global Solar Atlas 3.0, World Bank Group, 2024. Data provider: Solargis. License: CC BY 4.0. https://globalsolaratlas.info



\## Note on projection



Rasters are downloaded in their native geographic CRS (EPSG:4326). `scripts/01\_reproject\_clip.py` reprojects each one to its country-specific target CRS (see `crs\_target` in each config) before clipping - no manual reprojection is required before placing files here.
