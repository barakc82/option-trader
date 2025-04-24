import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { StateService } from '../../services/state.service';
import { State } from '../../models/state.model';

@Component({
  selector: 'app-state-view',
  templateUrl: './state-view.component.html',
  imports: [CommonModule]
})
export class StateViewComponent implements OnInit {
  state: State | null = null;

  constructor(private stateService: StateService) {}

  ngOnInit() {
    this.stateService.getState().subscribe((data) => {
      this.state = data;
    });
  }
}