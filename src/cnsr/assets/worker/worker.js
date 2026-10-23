import { connect } from "cloudflare:sockets";
import CHECK from "./qx-check.txt";
import CHECK_UI from "./qx-check-ui.txt";
import { makeHandler } from "./handler.js";

export default { fetch: makeHandler({ connect, scripts: { check: CHECK, checkUi: CHECK_UI } }) };
