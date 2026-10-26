import { mount } from "./mount";
import { SitePage } from "./site";

const page = document.getElementById("root")!.dataset.page ?? "404";
mount(<SitePage page={page} />);
