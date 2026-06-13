import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { StateService } from '../../services/state.service';
import { State } from '../../models/state.model';
import { SupervisorState } from '../../models/supervisor_state.model';

@Component({
  selector: 'app-state-view',
  standalone: true,
  templateUrl: './state-view.component.html',
  styleUrls: ['./state-view.component.scss'],
  imports: [CommonModule]
})
export class StateViewComponent implements OnInit {
  state: State | null = null;
  supervisor_state: SupervisorState | null = null;

  constructor(private stateService: StateService) {}

  ngOnInit() {
    this.stateService.state$.subscribe(data => {
      this.state = data;
    });

    this.stateService.fetchState();

    this.stateService.supervisorState$.subscribe(data => {
      this.supervisor_state = data;
    });

    this.stateService.fetchSupervisorState();
  }

  is_supervisor_active()
  {
    if (this.supervisor_state == null)
      return false;

    const now = Math.floor(Date.now() / 1000)
    return now - this.supervisor_state.time < 120;
    }

    has_spy_prices(): boolean {
      return !!this.state?.positions?.some(p => p.spy_price !== undefined);
    }

    has_es_prices(): boolean {
      return !!this.state?.positions?.some(p => p.es_price !== undefined);
    }
    }