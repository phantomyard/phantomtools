# Third-party attribution

The Page ID/token configuration and Graph Page operation structure (reading Page
data and creating a `/PAGE_ID/feed` post) were adapted from Hagai Hen's
[facebook-mcp-server](https://github.com/HagaiHen/facebook-mcp-server), inspected
2026-10-07. Relevant source: `config.py`, `facebook_api.py`, `manager.py`, `server.py`.
The standard-library transport, explicit link/hash validation, MCP implementation,
Phantombot injection setup and durable transaction handling were written for this
tool. We do not bundle the upstream server, dependencies or broad moderation tools.

Original license, preserved in full:

MIT License

Copyright (c) 2025 Hagai Hen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
