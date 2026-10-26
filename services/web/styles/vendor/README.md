# Vendored front-end code

Third-party files are copied into the repo so pages never load code from a CDN.

| Package | Version | Files | Source |
| --- | --- | --- | --- |
| [basecoat-css](https://basecoatui.com) (MIT) | 1.0.2 | `styles/vendor/basecoat/` (CSS source, build time only), `static/vendor/basecoat/` (`basecoat.min.js`, `sidebar.min.js`, `dropdown-menu.min.js`) | npm tarball `basecoat-css-1.0.2.tgz`, `dist/` folder |

The tarball was checked against the npm registry's integrity hash
`sha512-C6I5rr7HziIAoBYNGPO5U6HaU/6FuF3iVp++IgoEqqJ836is3XoRty6l1ibXqxnoIRZNnOAYDMX8QxVC+58zhw==`.
Only the Vega style and the three scripts the pages use were copied.

To upgrade, download the new tarball from the npm registry, check its
`dist.integrity`, replace these files, update this table, and run
`services/web/build-css.sh`.
