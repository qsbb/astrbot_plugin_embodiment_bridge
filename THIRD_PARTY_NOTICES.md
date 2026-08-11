# Third-party notices

This repository runs as an AstrBot plugin and uses third-party Python packages.
The root MPL-2.0 license covers original project source code only and does not
replace upstream terms.

## Runtime platform and dependencies

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) is distributed under
  AGPL-3.0. This plugin does not modify or vendor AstrBot Core.
- [Pydantic](https://github.com/pydantic/pydantic) is distributed under MIT.
- [aiohttp](https://github.com/aio-libs/aiohttp) declares Apache-2.0 AND MIT.
- [python-qrcode](https://github.com/lincolnloop/python-qrcode) is distributed
  under a BSD license.
- `audioop-lts` is a conditional Python 3.13+ compatibility dependency and
  remains subject to the license shipped by that package.

Dependency packages are installed separately from `requirements.txt`; their
source code is not copied into this repository.

## Referenced projects

The README links to public projects used to study event protocols, interruption,
asynchronous pipelines and embodied-client architecture. Unless explicitly
listed as a dependency above, those projects are references only: this
repository does not copy or redistribute their source code, models, motions,
audio, branding or sample assets.

Repositories without an explicit license, including Together Companion, are
restricted to high-level behavioral reference. No code may be copied from them
without separate permission.

## Plugin logo

`logo.png` was generated and supplied by the project owner, who authorized its
redistribution with this project as the plugin avatar. It is not Covered
Software under MPL-2.0 and may not be reused separately without permission.
