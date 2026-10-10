# Security policy

## Supported versions

Security fixes are released for the latest published version only.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through
[GitHub security advisories](https://github.com/carbotek/carbotek/security/advisories/new)
("Report a vulnerability" on the Security tab).

Include the carbotek version, the Python version and platform, and a minimal template and data
that reproduce the problem. You will get a reply within 7 days. Once a fix is released, the
advisory is published with credit to the reporter unless you ask otherwise.

## Threat model

carbotek is meant to render templates and data that may come from untrusted users. The following
are treated as vulnerabilities:

- reading local files or network resources that the configuration does not allow
  (`ImageSourceConfig`: `allowlist`, `file_root`), including through redirects, DNS answers,
  `file://` URIs, SVG `<image>` references or HTML `<img>` sources;
- a template or data value that makes the renderer evaluate data as a template marker
  (for example, a data string `{d.secret}` that is printed as the value of another field);
- XML external entities or document type declarations being processed;
- a crash of the process (not a Python exception), unbounded memory use or a hang caused by a
  template within the documented limits: 200 MiB uncompressed package size, image size limits,
  SVG rasterization limits and download timeouts.

Not in scope:

- CPU time that grows with the size of the data (a loop over a million rows takes time; limit the
  input size and run renders with a timeout in your service);
- behaviour that differs from Carbone but is not a security problem: please open a regular issue;
- vulnerabilities in dependencies (lxml, Pillow, dxpdf, resvg) that carbotek does not make
  reachable; report those to the respective project.

## Deployment recommendations

- Keep remote and local images disabled unless needed; when enabled, use a host allowlist and a
  dedicated `file_root`.
- Run rendering in worker processes with a time limit and a memory limit.
- Keep dependencies up to date, in particular lxml, Pillow and resvg.
