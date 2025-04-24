import { NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';

import { AppComponent } from './app.component';
import { StateViewComponent } from './components/state-view/state-view.component';


@NgModule({
  declarations: [AppComponent, StateViewComponent],
  imports: [BrowserModule],
  bootstrap: [AppComponent]
})
export class AppModule { }