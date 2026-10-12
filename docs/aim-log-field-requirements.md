# AIM log fields for LongLoopVE

Updated October 11, 2026. Scope: the MaxxECU Race v1 Alpha-N workflow using
ECU-calculated corrected VE (`CorVE`) values. Based on the current repository
and the owner's clarification of the intended analysis inputs.

## Minimum analysis inputs

**The four core inputs are RPM, electronic throttle-body position (ETPS),
CorVE for bank A, and CorVE for bank B.** RPM and ETPS locate the operating
point on the VE table; the two CorVE channels supply the proposed VE values.

| Signal | Exact channel in the existing profile | Units | Purpose |
| --- | --- | --- | --- |
| RPM | `RPM` | `rpm` | RPM table axis |
| ETPS | `ETPS_UC9` | `%` | Electronic throttle-body-position table axis |
| CorVE bank A | `CorVE_BankA_UC1` | `%` | ECU-calculated corrected VE for bank A |
| CorVE bank B | `CorVE_BankB_UC12` | `%` | ECU-calculated corrected VE for bank B |

These are the minimum signal inputs for the intended **CorVE-based method**,
assuming the ECU math channels correctly include the intended fuel corrections.
The supplied expression below still needs verification in the actual tune and
recordings. This is not a universal
minimum for other VE calculation methods.

AIM `TPS` is pedal position in this setup. It cannot replace ETPS as the table
axis. MAP is diagnostic context rather than a table axis for this Alpha-N tune.

Each signal needs usable values and timestamps covering the same operating
periods. Four channel names alone do not establish sufficient data quality,
coverage, or a complete table grid.

## CorVE math channels in MaxxECU: purpose and setup

`CorVE_BankA` and `CorVE_BankB` are custom math channels configured in MTune.
They calculate a proposed VE value at the current operating point for each
bank. They do not write that value into the VE table. The intention is to fold
the bank's current VE, long-term trim (LTT), and active short-term/lambda fuel
correction into one logged value that LongLoopVE can associate with RPM and ETPS.

### Calculation and percentage conventions

For corrections that represent separate multiplicative fuel factors, the
intended calculation is:

```text
CorVE = CurrentVE * (1 + ShortTermCorrection / 100)
                  * (1 + LongTermAdaptation / 100)
                  * MeasuredLambda / TargetLambda
```

### Mini Script

Assign inputs `A`–`E` separately for each bank using the comments below.

```te
-- A = Current VE
-- B = Measured lambda
-- C = Target lambda
-- D = Short term lambda correction (%)
-- E = Long term fuel adaptation (%)

-- Replace this RTDATA name with the actual
-- fuel acceleration enrichment channel
-- selected using MTune autocomplete.
local ae = RTDATA.ACC_ENRICHMENT

-- Reject acceleration enrichment samples
-- and allow lambda readings to settle.
if ABS(ae) > 0.01 then
    SETTMR(0, 0.15)
end

if GETTMR(0) > 0 then
    return -1
end

-- Reject invalid inputs
if ISNAN(A) == 1 or ISNAN(B) == 1 or
   ISNAN(C) == 1 or ISNAN(D) == 1 or
   ISNAN(E) == 1 then
    return -1
end

if A <= 0 or B <= 0 or C <= 0 then
    return -1
end

return A * (1 + D/100) *
           (1 + E/100) * B/C
```

While `ABS(ae) > 0.01`, the script repeatedly starts a `0.15`-second timer.
It returns `-1` while that timer is active, including the settling period after
enrichment stops. It also returns `-1` for the invalid inputs explicitly checked
above. **Treat `-1` as a rejected sample, never as a VE proposal.** Current
ingestion preserves this finite value; rejection must be implemented by analysis.

MaxxECU Mini Script timers are shared between expressions. Reserve timer `0`
for this purpose. Both bank scripts can intentionally share it if they use the
same enrichment signal and should reject together. Otherwise assign separate
unused timer indices and update both timer calls in each script. Check other
scripts for timer conflicts. These timer semantics are documented on the
[Math channels page](https://www.maxxecu.com/webhelp/settings-advanced-math_channels.html).


## Signals for rejecting contaminated samples

**Record these four additional signals to support sample rejection.** They do
not provide the table coordinates or proposed VE values, but they help determine
whether a CorVE sample is suitable to use.

| Signal | Exact channel in the existing profile | Units | Intended rejection use |
| --- | --- | --- | --- |
| Acceleration enrichment | `AccelEnric_UC10` | `%` | Identify transient enrichment that can contaminate the inferred VE |
| Coolant temperature | `CoolantTemp` | `C` | Identify warm-up or operation outside the selected temperature conditions |
| Intake air temperature | `IntakeAirTemp` | `C` | Identify operation outside the selected intake-temperature conditions |
| Fuel cut | `FuelCut` | `%` | Exclude fuel-cut operation |

This gives a practical logging set of **eight signals: four core inputs plus
four rejection signals**. Missing rejection signals reduce the ability to
screen contaminated inputs; their absence should not be interpreted as proof
that enrichment, fuel cut, or unsuitable temperatures were absent.

The supplied ECU script handles enrichment using a `0.01` threshold and a
`0.15`-second settling timer. It does not check coolant temperature, intake air
temperature, or fuel cut. The current LongLoopVE code does not implement these
filters or reject the script's `-1` sentinel. Additional rejection thresholds,
percentage conventions (including what indicates inactive enrichment or fuel
cut), temperature windows, and any exclusion period around transitions must be
confirmed from the ECU settings and recorded behavior. No numerical thresholds
are prescribed here beyond the supplied script's enrichment check.

## Additional diagnostic and validation evidence

The following channels are useful for auditing the ECU's CorVE calculations,
explaining proposed changes, and investigating problems. They are not separate
hard inputs to the intended method once the CorVE expressions are verified.

| Channel(s) in the existing profile | Units | Evidence supplied |
| --- | --- | --- |
| `VE_Bank_A_UC5`, `VE_Bank_B_UC6` | `%` | Current bank VE values |
| `Lambda_A`, `Lambda_B` | `lambda` | Measured mixture per bank |
| `Lambda_Target` | `lambda` | Target mixture |
| `Lambda_corr_A`, `Lambda_corr_B` | `%` | Per-bank lambda corrections |
| `LTT_Bank_A_UC7`, `LTT_Bank_B_UC8` | `%` | Per-bank long-term trims |
| `TotFuelTrim_UC2`, `FuelTrimTotal` | `%` | Total-trim context |
| `TPS` | `%` | Pedal-position context |
| `MAP`, `BaroPress` | `bar` | Pressure context; MAP pressure convention remains unconfirmed |

CorVE is intended to incorporate current VE and trim/lambda correction in the
ECU. Do not apply those corrections again to CorVE without checking the actual
math. The interpolation behavior of the logged current VE values and the
physical bank/cylinder assignment remain unverified.
## 

## Time and quality requirements

- Input must be a nonempty, decodable AIM `.xrk` or `.xrz` file. The decoder must
  return at least one channel containing samples.
- Decoder channel tables must contain `timecodes` and the named value column.
  Timecodes must have an integer type and represent milliseconds. Units are read
  from the value field's Arrow metadata; these are not extra logger channels.
- Ingestion preserves each channel's decoded timeline, values, units, and
  metadata. It does not sort, align, interpolate, resample, convert units, or
  apply additional calibration. The decoder may apply its own calibration or
  timing corrections.
- Null timestamps/values, non-finite values, reversed/duplicate timestamps, and
  large gaps are reported. These findings do not automatically reject samples
  or fail an otherwise valid profile check.
- There is no implemented minimum sample rate, session duration, common time
  coverage, table-cell coverage, or acceptable operating range. Reported rates
  are estimates; gaps over five median intervals are descriptive warnings.

Analysis must eventually align the core and rejection signals explicitly and
handle gaps and timing relationships. Passing presence and unit checks does not
show that a sample is clean or usable.


