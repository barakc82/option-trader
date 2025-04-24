import { Component } from '@angular/core';
import { StateViewComponent } from './components/state-view/state-view.component';

@Component({
  selector: 'app-root',
  imports: [StateViewComponent],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.scss']
})
export class AppComponent {
  title = 'Live State Dashboard';
}