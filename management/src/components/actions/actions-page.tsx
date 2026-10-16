import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { Eye, Router, ShieldCheck, Wrench } from 'lucide-react';
import { PageHeader } from '#/components/layout/application-shell';
import {
  CapabilityGap,
  SectionHeading,
} from '#/components/shared/presentation';
import { Badge } from '#/components/ui/badge';
import { Button } from '#/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '#/components/ui/card';
import { fetchStateAgentInspectionAction } from '#/infrastructure/http/operations-client';

export function ActionsPage() {
  const actionQuery = useQuery({
    queryKey: ['actions', 'state-agent-inspection'],
    queryFn: ({ signal }) => fetchStateAgentInspectionAction(signal),
  });
  const action = actionQuery.data;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Reusable intent"
        title="Actions"
        description="Review supported outcomes, applicability, and risk before an operation is accepted."
      />

      <section aria-labelledby="inspect-actions-title">
        <SectionHeading
          eyebrow="Inspect & verify"
          title="Available capabilities"
        />
        <Card className="action-catalog-card" id="inspect-actions-title">
          <CardHeader>
            <div className="card-heading-row">
              <span className="card-icon" aria-hidden="true">
                <Eye />
              </span>
              <div>
                <CardTitle>Inspect State Agent health</CardTitle>
                <CardDescription>
                  Target: configured State Agent · read-only
                </CardDescription>
              </div>
              {actionQuery.isPending ? (
                <Badge variant="outline">Checking</Badge>
              ) : action?.available ? (
                <Badge className="status-ready">Available</Badge>
              ) : (
                <Badge className="status-warning">Unavailable</Badge>
              )}
            </div>
          </CardHeader>
          <CardContent>
            <p className="card-copy">
              {actionQuery.error?.message ??
                action?.unavailableReason ??
                action?.description ??
                'Checking capability availability.'}
            </p>
            <div className="action-card-footer">
              <span>
                <ShieldCheck aria-hidden="true" /> No feeder writes
              </span>
              {action?.available ? (
                <Button asChild>
                  <Link to="/operations">Open action</Link>
                </Button>
              ) : (
                <Button disabled>Open action</Button>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="installation-actions-title">
        <SectionHeading eyebrow="Install & recover" title="Guided workflows" />
        <Card className="action-catalog-card" id="installation-actions-title">
          <CardHeader>
            <div className="card-heading-row">
              <span className="card-icon" aria-hidden="true">
                <Router />
              </span>
              <div>
                <CardTitle>Install PLAF203</CardTitle>
                <CardDescription>
                  Durable workflow · device-changing · qualification gated
                </CardDescription>
              </div>
              <Badge className="status-warning">Controlled use</Badge>
            </div>
          </CardHeader>
          <CardContent>
            <p className="card-copy">
              Preflight, protected artifacts, identity capture, final-broker
              proof, guarded OTA, configuration handoff, and source-backed
              readiness verification share one durable operation.
            </p>
            <div className="action-card-footer">
              <span>
                <ShieldCheck aria-hidden="true" /> No blind replay after
                uncertainty
              </span>
              <Button asChild>
                <a href={`${import.meta.env.BASE_URL}setup`}>
                  Open guided setup
                </a>
              </Button>
            </div>
          </CardContent>
        </Card>
      </section>

      <CapabilityGap
        compact
        icon={Wrench}
        title="Independent device-changing recovery remains qualification gated"
        description="Safe checkpoint continuation and runtime re-verification are available inside installation. Arbitrary OTA retry, broker mutation, shell access, and routine feeder writes are intentionally unavailable."
      />
    </div>
  );
}
