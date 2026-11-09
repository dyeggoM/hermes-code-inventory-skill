import { Component } from "@angular/core";
import { formatDate } from "../util/format";

@Component({ selector: "app-hero", template: "<h1>{{ title }}</h1>" })
export class HeroComponent {
  title = "Hero";

  ngOnInit(): void {
    this.title = formatDate(new Date());
  }

  reset(): void {
    this.title = "Hero";
  }
}
