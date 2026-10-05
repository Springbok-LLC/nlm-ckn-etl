# Publications and cell set datasets — NLM-CKN v1.0.0-rc.7

Prepared 2026-07-28 from the `Cell-KN-Phenotypes` database reached over the
`arango-tunnel.sh` SSM port-forward at `localhost:8530`
(`nlm-ckn-dev-arangodb`; there is no prod ArangoDB stack in the account —
CloudFormation has only `nlm-ckn-dev-arangodb` and `nlm-ckn-stage-arangodb`,
and `arango-tunnel.sh` still uses the stale `cell-kn-<env>` stack/secret names).
The graph holds the current production data release, **v1.0.0-rc.7**
(`release.json` → `tar_source`; published 2026-07-17 in `Springbok-LLC/nlm-ckn`).

Collection counts, queried directly:

| Collection | Documents |
|---|---|
| `CSD` (cell set datasets) | 89 |
| `PUB` (publications) | 59 |
| `CSD-PUB` (attribution edges) | 89 |

Every cell set dataset is attributed to exactly one publication. These counts and
the full key sets match the tuples generated for the release
(`data/tuples-1.0.0-rc.7/cellxgene.json`) exactly — 0 differences in `CSD` keys,
`PUB` keys, or the `CSD → PUB` mapping.

`CSD` keys use the composite form `<dataset-version-uuid>__<anatomical_structure>`,
so a single CELLxGENE dataset filtered to several tissues yields several `CSD`
vertices.

## Table 1 — All publications

| # | Citation | DOI | CSDs |
|---|---|---|---|
| 1 | Andrews (2022) Hepatol Commun | 10.1002/hep4.1854 | 1 |
| 2 | Andrews (2024) Journal of Hepatology | 10.1016/j.jhep.2023.12.023 | 2 |
| 3 | Berg (2025) Journal of Cystic Fibrosis | 10.1016/j.jcf.2025.01.016 | 1 |
| 4 | Burclaff (2022) Cellular and Molecular Gastroenterology and Hepatology | 10.1016/j.jcmgh.2022.02.007 | 1 |
| 5 | Cheng (2018) Cell Reports | 10.1016/j.celrep.2018.09.006 | 1 |
| 6 | Domínguez Conde (2022) Science | 10.1126/science.abl5197 | 3 |
| 7 | Edgar (2025) Hepatology Communications | 10.1097/hc9.0000000000000813 | 2 |
| 8 | Elmentaite (2021) Nature | 10.1038/s41586-021-03852-1 | 1 |
| 9 | Eraslan (2022) Science | 10.1126/science.abl4290 | 4 |
| 10 | Fasolino (2022) Nat Metab | 10.1038/s42255-022-00531-x | 1 |
| 11 | Gao (2021) Cell Death Dis | 10.1038/s41419-021-03724-6 | 1 |
| 12 | Garrido-Trigo (2023) Nat Commun | 10.1038/s41467-023-40156-6 | 1 |
| 13 | Guilliams (2022) Cell | 10.1016/j.cell.2021.12.018 | 1 |
| 14 | Guo (2023) Nat Commun | 10.1038/s41467-023-40173-5 | 1 |
| 15 | Han (2020) Nature | 10.1038/s41586-020-2157-4 | 7 |
| 16 | Horeth (2023) J Dent Res | 10.1177/00220345221147908 | 1 |
| 17 | Hulsmans (2023) Science | 10.1126/science.abq3061 | 1 |
| 18 | Jorstad (2023) Science | 10.1126/science.adf6812 | 5 |
| 19 | Lake (2023) Nature | 10.1038/s41586-023-05769-3 | 1 |
| 20 | Litviňuková (2020) Nature | 10.1038/s41586-020-2797-4 | 1 |
| 21 | Maatz (2025) Nat Cardiovasc Res | 10.1038/s44161-025-00612-6 | 1 |
| 22 | MacParland (2018) Nat Commun | 10.1038/s41467-018-06318-7 | 1 |
| 23 | Madissoon (2020) Genome Biol | 10.1186/s13059-019-1906-x | 2 |
| 24 | Madissoon (2023) Nat Genet | 10.1038/s41588-022-01243-4 | 1 |
| 25 | Martin (2019) Cell | 10.1016/j.cell.2019.08.008 | 1 |
| 26 | McEvoy (2022) Nat Commun | 10.1038/s41467-022-35297-z | 1 |
| 27 | Melms (2021) Nature | 10.1038/s41586-021-03569-1 | 1 |
| 28 | Muraro (2016) Cell Systems | 10.1016/j.cels.2016.09.002 | 1 |
| 29 | Muto (2021) Nat Commun | 10.1038/s41467-021-22368-w | 1 |
| 30 | Natri (2024) Nat Genet | 10.1038/s41588-024-01702-0 | 1 |
| 31 | Nowicki-Osuch (2023) Cancer Discovery | 10.1158/2159-8290.cd-22-0824 | 1 |
| 32 | Oliver (2024) Nature | 10.1038/s41586-024-07571-1 | 1 |
| 33 | Reck (2025) Nat Commun | 10.1038/s41467-025-59997-4 | 1 |
| 34 | Reynolds (2021) Science | 10.1126/science.aba6500 | 1 |
| 35 | Rustam (2023) American Journal of Respiratory and Critical Care Medicine | 10.1164/rccm.202207-1384oc | 1 |
| 36 | Selewa (2023) Nat Commun | 10.1038/s41467-023-40505-5 | 1 |
| 37 | Sikkema (2023) Nat Med | 10.1038/s41591-023-02327-2 | 1 |
| 38 | Sim (2021) Circulation | 10.1161/circulationaha.120.051921 | 1 |
| 39 | Smillie (2019) Cell | 10.1016/j.cell.2019.06.029 | 1 |
| 40 | Solé-Boldo (2020) Commun Biol | 10.1038/s42003-020-0922-4 | 1 |
| 41 | Stewart (2019) Science | 10.1126/science.aat5031 | 1 |
| 42 | Szabo (2019) Nat Commun | 10.1038/s41467-019-12464-3 | 1 |
| 43 | Travaglini (2020) Nature | 10.1038/s41586-020-2922-4 | 2 |
| 44 | Triana (2021) Nat Immunol | 10.1038/s41590-021-01059-0 | 1 |
| 45 | Tritschler (2022) Molecular Metabolism | 10.1016/j.molmet.2022.101595 | 1 |
| 46 | Wang (2020) eLife | 10.7554/elife.62522 | 1 |
| 47 | Watanabe (2022) American Journal of Respiratory Cell and Molecular Biology | 10.1165/rcmb.2021-0555oc | 1 |
| 48 | Wells (2025) Nat Immunol | 10.1038/s41590-025-02241-4 | 4 |
| 49 | Wiedemann (2023) Cell Reports | 10.1016/j.celrep.2023.111994 | 1 |
| 50 | Wilson (2022) Nat Commun | 10.1038/s41467-022-32972-z | 1 |
| 51 | Wong (2024) American Journal of Transplantation | 10.1016/j.ajt.2024.08.019 | 1 |
| 52 | Xu (2022) Sci Rep | 10.1038/s41598-022-17832-6 | 2 |
| 53 | Xu (2023) Cell | 10.1016/j.cell.2023.11.026 | 8 |
| 54 | Yoshida (2022) Nature | 10.1038/s41586-021-04345-x | 1 |
| 55 | Young (2018) Science | 10.1126/science.aat1699 | 1 |
| 56 | Zeng (2025) Blood Cancer Discovery | 10.1158/2643-3230.bcd-24-0342 | 1 |
| 57 | Zhao (2025) Cell Genomics | 10.1016/j.xgen.2025.101034 | 1 |
| 58 | Ziegler (2021) Cell | 10.1016/j.cell.2021.07.023 | 1 |
| 59 | Zwick (2024) Nat Cell Biol | 10.1038/s41556-023-01337-z | 1 |

Total: **59 publications**, **89 cell set datasets**.

## Table 2 — Publications with more than one cell set dataset

11 of the 59 publications have more than one cell set dataset,
accounting for 41 of the 89 CSDs.

| Citation | CSDs | Organs (`anatomical_structure`) | Organs distinguish? |
|---|---|---|---|
| Andrews (2024) Journal of Hepatology | 2 | liver ×2 | **No** — snRNA-seq `4b5895d7` vs scRNA-seq `7d4d0da4` |
| Domínguez Conde (2022) Science | 3 | bone_marrow, liver, respiratory_system | Yes (keys) — one dataset (`78819b62`) split across 3 organs; see note 1 |
| Edgar (2025) Hepatology Communications | 2 | liver ×2 | **No** — healthy `10cc50a0` vs healthy+IFALD `feca90bb` |
| Eraslan (2022) Science | 4 | digestive_tract, heart_plus_pericardium, respiratory_system, skin_of_body | Yes (keys) — one dataset (`002308e1`) split across 4 organs; see note 1 |
| Han (2020) Nature | 7 | bone_marrow, digestive_tract, heart_plus_pericardium, kidney, liver, pancreas, respiratory_system | Yes (keys) — but one dataset (`74c3403a`) split across 7 organs; see note 1 |
| Jorstad (2023) Science | 5 | neocortex ×5 | **No** — 5 distinct datasets, all neocortex (superclusters) |
| Madissoon (2020) Genome Biol | 2 | digestive_tract, respiratory_system | Yes — 2 genuinely distinct datasets (Esophagus Epithelium; Lung Parenchyma) |
| Travaglini (2020) Nature | 2 | respiratory_system ×2 | **No** — 10X `fb57b6c0` vs Smart-seq2 `c88e0403` |
| Wells (2025) Nat Immunol | 4 | bone_marrow, liver, respiratory_system, skin_of_body | Yes (keys) — one dataset (`7c98dadf`) split across 4 organs; see note 1 |
| Xu (2022) Sci Rep | 2 | respiratory_system ×2 | **No** — bronchial `167280f7` vs nasal `9f6f40d4` |
| Xu (2023) Cell | 8 | bone_marrow, digestive_tract, heart_plus_pericardium, kidney, liver, pancreas, respiratory_system, retina | Yes — 7 distinct datasets across 8 keys; see note 2 |

Organ alone does **not** disambiguate 5 of the 11: for Jorstad, Andrews 2024,
Edgar, Travaglini and Xu 2022 the multiple datasets sit in the *same* organ and
are separated only by the dataset-version uuid in the key — different
superclusters, assay, sampling site, or disease cohort. The keys remain unique;
it is the human-readable label that collides.

## Note 1 — cross-tissue splits duplicate the same filtered result

For the four publications where one CELLxGENE dataset is keyed under several
organs (Han ×7, Eraslan ×4, Wells ×4, Domínguez Conde ×3), every organ variant
carries **identical** `filtered_cell_count`, `tissue_annotation` and
`cluster_annotation`, and the `tissue_annotation` names a single UBERON term
across all of them:

| Citation | Dataset version | Organs | `filtered_cell_count` | `tissue_annotation` |
|---|---|---|---|---|
| Han (2020) Nature | `74c3403a-451c-4a62-84e0-d8a8e45c7ea7` | 7 | 8 704 | `UBERON:0002371: 8704` (bone marrow) |
| Eraslan (2022) Science | `002308e1-0121-4aa1-b8f2-9d034cf44b0f` | 4 | 34 173 | `UBERON:0004648: 34173` |
| Wells (2025) Nat Immunol | `7c98dadf-b249-4873-83be-51776674e9e1` | 4 | 142 608 | `UBERON:0002371: 142608` |
| Domínguez Conde (2022) Science | `78819b62-0699-4672-8dc8-d9317b04d255` | 3 | 40 507 | `UBERON:0002371: 40507` |

So the organ suffix distinguishes the keys but not the underlying cell content:
those 18 CSD vertices represent 4 filtered results. Worth reviewing against the
intent of "Key cell set datasets per filtered dataset, not per source"
(commit 45810dd) — e.g. Han's kidney and pancreas keys both carry the bone-marrow
tissue annotation.

## Note 2 — Xu (2023) Cell retina entry looks mis-assigned

Dataset `ad9529a3-0937-4177-9732-31dee15188c1` exists as both
`…__bone_marrow` and `…__retina`. Both carry `dataset_name: "Bone_marrow"`,
`tissue_annotation: UBERON:0002371` (bone marrow) and 66 613 cells, differing
only in `cluster_annotation` (`Curated_annotation` vs `author_cell_type`). Xu
2023's 8 CSDs therefore come from 7 distinct datasets. The suspect input is
`results_ensg_retina_Xu_Cell_2023_author_cell_type_X_umap_5188c1`, whose
`5188c1` suffix is shared with the bone_marrow Xu 2023 run.

## Note 3 — attribute shapes in `CSD`

The 89 `CSD` documents have 5 distinct attribute sets:

- 76 carry the full NSForest summary (`filtered_cell_count`, `tissue_annotation`,
  `mean_f_beta_score`, `mean_silhouette`, `donor_id_count`, `assay_summary`, …).
- 8 carry `cluster_annotation` and the f-beta/silhouette stats but no
  `filtered_cell_count` and no `tissue_annotation`.
- 5 (the Jorstad neocortex superclusters) carry no `cluster_annotation` and no
  NSForest stats at all.
- 15 carry a `publication` attribute the other 74 lack (including all 5 Jorstad).

`cell_count` is the **source** dataset total; `filtered_cell_count` is the
per-run filtered value. They differ substantially — e.g. Han 599 926 source vs
8 704 filtered; Wells 1 281 499 vs 142 608; Eraslan 209 126 vs 34 173;
Domínguez Conde 329 762 vs 40 507.
