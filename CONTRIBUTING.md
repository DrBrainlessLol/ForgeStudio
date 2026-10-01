# Contributing to Forge Studio

Thanks for helping! A few things to know:

- **License.** Forge Studio is licensed under the [GNU AGPL-3.0](LICENSE). Your contributions are released under it too.
- **CLA.** First-time contributors are asked to sign the [Contributor License Agreement](CLA.md) by commenting on their pull request (a bot will prompt you). It lets the project also offer commercial licenses, which helps fund development; you keep the copyright in your work.
- **Keep it light.** The server uses only the Python standard library and the UI is plain HTML/CSS/JS with no build step. Please don't add frameworks or dependencies without discussing it in an issue first.
- **Check before you send.** `python3 -m py_compile server.py` and `node --check static/*.js` should pass, and please describe how you tested your change.
