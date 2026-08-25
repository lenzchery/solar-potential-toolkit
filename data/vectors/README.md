\# data/vectors/



This folder holds the per-country administrative boundary shapefiles used by the pipeline. \*\*Not version-controlled\*\* (see `.gitignore`) - most of these sources restrict redistribution, so each user must download them independently under the provider's own terms.



\## Sources by country



| Country | Provider | Dataset | License | Download |

|---|---|---|---|---|

| Haïti | OCHA Haiti (source: CNIGS) | Subnational Administrative Boundaries (COD-AB), ADM2 | CC BY-IGO | https://data.humdata.org/dataset/cod-ab-hti |

| La Réunion | GADM | Second-level Administrative Divisions (v2.8) | Academic/non-commercial only; redistribution requires prior permission | https://purl.stanford.edu/pn417fx4462 |

| Madagascar | GADM | Administrative Divisions, level 2 (v4.1) | Academic/non-commercial only; redistribution requires prior permission | https://gadm.org/download\_country.html |

| Maroc | GADM | Administrative Divisions, level 2 (v4.1) | Academic/non-commercial only; redistribution requires prior permission | https://gadm.org/download\_country.html |

| Kenya | GADM | Administrative Divisions, level 2 (v4.1) | Academic/non-commercial only; redistribution requires prior permission | https://gadm.org/download\_country.html |

| Sénégal | GADM | Administrative Divisions, level 2 (v4.1) | Academic/non-commercial only; redistribution requires prior permission | https://gadm.org/download\_country.html |



\## Expected filenames



| Country | Expected filename(s) |

|---|---|

| Haïti | `hti\_admin2\_em.shp` (+ `.dbf`, `.shx`, `.prj`, ...) |

| La Réunion | `REU\_adm2.shp` (+ sidecars) |

| Madagascar | `gadm41\_MDG\_2.shp` (+ sidecars) |

| Maroc | `gadm41\_MAR\_2.shp` (+ sidecars) |

| Kenya | `gadm41\_KEN\_2.shp` (+ sidecars) |

| Sénégal | `gadm41\_SEN\_2.shp` (+ sidecars) |



Place each downloaded shapefile (all its sidecar files: `.shp`, `.dbf`, `.shx`, `.prj`, etc.) directly in this folder - filenames must match `admin\_vector\_filename` in the corresponding `config/<country>.json`.



\## Important: GADM license



Four of the six countries (Réunion, Madagascar, Maroc, Kenya, Sénégal) use \*\*GADM\*\* data, whose license states:



> "The data are freely available for academic use and other non-commercial use. Redistribution or commercial use is not allowed without prior permission."



This is why these files are excluded from version control. Do not commit them to a fork or derivative repository without checking GADM's current terms.



\## Note on GADM version heterogeneity



Réunion uses \*\*GADM v2.8\*\* (2015); Madagascar, Maroc, Kenya, and Sénégal use \*\*GADM v4.1\*\*. These versions differ in schema and methodology - see the main README's data vintage note before treating cross-country administrative figures as directly comparable.
