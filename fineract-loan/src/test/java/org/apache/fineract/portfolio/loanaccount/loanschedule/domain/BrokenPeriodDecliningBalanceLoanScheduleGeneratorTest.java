/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.fineract.portfolio.loanaccount.loanschedule.domain;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.TreeMap;
import org.apache.fineract.infrastructure.core.exception.GeneralPlatformDomainRuleException;
import org.apache.fineract.organisation.monetary.domain.MonetaryCurrency;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.domain.MoneyHelper;
import org.apache.fineract.organisation.monetary.mapper.CurrencyMapper;
import org.apache.fineract.portfolio.common.domain.PeriodFrequencyType;
import org.apache.fineract.portfolio.loanaccount.domain.LoanTransactionRepository;
import org.apache.fineract.portfolio.loanaccount.loanschedule.service.BrokenPeriodMethodResolver;
import org.apache.fineract.portfolio.loanproduct.domain.InterestCalculationPeriodMethod;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.AdditionalAnswers;
import org.mockito.MockedStatic;

/**
 * Pinned-value tests for the per-product broken-period first-installment math. The stock (super) computation is driven
 * through a mocked {@link LoanApplicationTerms} whose interest/principal calculators return the plain 30-day-month
 * values, matching what the real cumulative generator produces under SAME_AS_REPAYMENT_PERIOD.
 *
 * Golden vectors come from DMI_Logic_Validation.xlsx (5,143 real DMI schedules) and the Vivriti parity harness.
 */
public class BrokenPeriodDecliningBalanceLoanScheduleGeneratorTest {

    // Tenant convention: rounding-mode 4 (HALF_UP), INR with 2 decimals.
    private static final MockedStatic<MoneyHelper> MONEY_HELPER = mockStatic(MoneyHelper.class);
    private static final MathContext MC = new MathContext(19, RoundingMode.HALF_UP);
    private static final MonetaryCurrency INR = new MonetaryCurrency("INR", 2, 0);
    private static final Long PRODUCT_ID = 7L;

    private BrokenPeriodMethodResolver resolver;
    private LoanApplicationTerms terms;
    private BrokenPeriodDecliningBalanceLoanScheduleGenerator generator;

    @BeforeEach
    public void setUp() {
        MONEY_HELPER.when(MoneyHelper::getRoundingMode).thenReturn(RoundingMode.HALF_UP);
        MONEY_HELPER.when(MoneyHelper::getMathContext).thenReturn(MC);
        resolver = mock(BrokenPeriodMethodResolver.class);
        terms = mock(LoanApplicationTerms.class);
        generator = new BrokenPeriodDecliningBalanceLoanScheduleGenerator(mock(ScheduledDateGenerator.class),
                mock(PaymentPeriodsInOneYearCalculator.class), mock(LoanTransactionRepository.class), mock(CurrencyMapper.class), resolver);
    }

    @AfterAll
    public static void tearDown() {
        MONEY_HELPER.close();
    }

    private void givenLoan(final BigDecimal principal, final BigDecimal annualRate, final int tenure, final LocalDate interestStart,
            final String method) {
        when(resolver.resolve(PRODUCT_ID)).thenReturn(method);
        when(terms.getLoanProductId()).thenReturn(PRODUCT_ID);
        when(terms.getInterestCalculationPeriodMethod()).thenReturn(InterestCalculationPeriodMethod.SAME_AS_REPAYMENT_PERIOD);
        when(terms.getExpectedDisbursementDate()).thenReturn(interestStart);
        when(terms.getAnnualNominalInterestRate()).thenReturn(annualRate);
        when(terms.getNumberOfRepayments()).thenReturn(tenure);
        when(terms.getPrincipalGrace()).thenReturn(0);
        // Stock SAME_AS_REPAYMENT period-1 values: interest = OB * rate/1200, principal = unroundedEMI - interest.
        final BigDecimal monthlyRate = annualRate.divide(BigDecimal.valueOf(1200), MC);
        final BigDecimal stockInterest = principal.multiply(monthlyRate, MC);
        final BigDecimal pow = BigDecimal.ONE.add(monthlyRate).pow(tenure, MC);
        final BigDecimal emiUnrounded = principal.multiply(monthlyRate, MC)
                .divide(BigDecimal.ONE.subtract(BigDecimal.ONE.divide(pow, MC)), MC);
        when(terms.calculateTotalInterestForPeriod(any(), any(), anyInt(), any(), any(), any(), any(), any()))
                .thenReturn(new PrincipalInterest(Money.zero(INR, MC), Money.of(INR, stockInterest, MC), Money.zero(INR, MC)));
        when(terms.calculateTotalPrincipalForPeriod(any(), any(), anyInt(), any(), any()))
                .thenReturn(Money.of(INR, emiUnrounded.subtract(stockInterest), MC));
        when(terms.adjustPrincipalIfLastRepaymentPeriod(any(), any(), anyInt())).thenAnswer(AdditionalAnswers.returnsFirstArg());
    }

    private PrincipalInterest generate(final BigDecimal principal, final LocalDate periodStart, final LocalDate periodEnd,
            final int periodNumber) {
        final Money outstanding = Money.of(INR, principal, MC);
        return generator.calculatePrincipalInterestComponentsForPeriod(mock(PaymentPeriodsInOneYearCalculator.class), BigDecimal.ZERO,
                Money.zero(INR, MC), Money.zero(INR, MC), Money.zero(INR, MC), Money.zero(INR, MC), outstanding, terms, periodNumber, MC,
                new TreeMap<>(), new HashMap<>(), periodStart, periodEnd, new ArrayList<>());
    }

    // --- DMI: ADJUSTED_DAYS_SYMMETRIC ---

    @Test
    public void dmiLongBrokenPeriodSurchargesFirstInstallment() {
        // xlsx golden: 312000 @22%/36m, interest start 21-Apr-2026 (30d month), first coupon 05-Jun-2026:
        // raw 45 days, adjusted 45; EMI 11915.42, first installment 14775.42 (interest 8580.00, principal 6195.42).
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        assertEquals(new BigDecimal("8580.00"), result.interest().getAmount());
        assertEquals(new BigDecimal("6195.42"), result.principal().getAmount());
    }

    @Test
    public void dmiShortBrokenPeriodShrinksFirstInstallmentBelowEmi() {
        // xlsx golden: 320000 @23%/36m, interest start 14-Jul-2025 (31d month -> -1), first coupon 05-Aug-2025:
        // raw 22 days, adjusted 21; EMI 12387.11, first installment 10547.11 (interest 4293.33, principal 6253.78).
        final BigDecimal principal = BigDecimal.valueOf(320000);
        givenLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2025, 7, 14), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 7, 5), LocalDate.of(2025, 8, 5), 1);
        assertEquals(new BigDecimal("4293.33"), result.interest().getAmount());
        assertEquals(new BigDecimal("6253.78"), result.principal().getAmount());
    }

    @Test
    public void dmiFebruaryStartAddsTwoDays() {
        // 300000 @22%/36m, interest start 10-Feb-2026 (28d month -> +2), first coupon 05-Mar-2026:
        // raw 23 days, adjusted 25; interest = 300000*22/36000*25 = 4583.33.
        final BigDecimal principal = BigDecimal.valueOf(300000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 2, 10), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 2, 5), LocalDate.of(2026, 3, 5), 1);
        assertEquals(new BigDecimal("4583.33"), result.interest().getAmount());
    }

    @Test
    public void dmiAdjustedThirtyDayBoundaryReturnsStock() {
        // Interest start 05-Dec-2026 (31d month): raw 31, adjusted 30 -> stock result must be returned as-is.
        final BigDecimal principal = BigDecimal.valueOf(300000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 12, 5), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 12, 5), LocalDate.of(2027, 1, 5), 1);
        // stock 30-day interest = 300000*22/1200 = 5500.00
        assertEquals(new BigDecimal("5500.00"), result.interest().getAmount());
    }

    @Test
    public void dmiAnchorsOnInterestChargedFromDateWhenPresent() {
        // Disbursed 18-Apr but interest runs from 21-Apr: same numbers as the long-broken-period golden.
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 18), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        when(terms.getInterestChargedFromLocalDate()).thenReturn(LocalDate.of(2026, 4, 21));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        assertEquals(new BigDecimal("8580.00"), result.interest().getAmount());
        assertEquals(new BigDecimal("6195.42"), result.principal().getAmount());
    }

    // --- Legacy (null method) and Vivriti (ACTUAL_DAYS_SURCHARGE) ---

    @Test
    public void legacyShortBrokenPeriodKeepsInstallmentEqualToEmi() {
        // Vivriti-style: 420000 @21.18%/24m disbursed 10-Jan-2025, first EMI 05-Feb-2025 -> 26 actual days.
        // Interest = 420000*21.18/36000*26 = 6424.60; installment stays = stock EMI (interest saved moves to
        // principal).
        final BigDecimal principal = BigDecimal.valueOf(420000);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), null);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        assertEquals(new BigDecimal("6424.60"), result.interest().getAmount());
        // installment == stock EMI: principal + interest must equal the stock principal + stock 30-day interest
        final BigDecimal stockEmi = new BigDecimal("14206.12").add(new BigDecimal("7413.00")); // unroundedEMI split
                                                                                               // @21.18%/24m
        assertEquals(stockEmi, result.principal().getAmount().add(result.interest().getAmount()));
    }

    @Test
    public void actualDaysSurchargeMatchesLegacyBehaviour() {
        final BigDecimal principal = BigDecimal.valueOf(420000);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), BrokenPeriodMethodResolver.ACTUAL_DAYS_SURCHARGE);
        final PrincipalInterest explicit = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), null);
        final PrincipalInterest legacy = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        assertEquals(legacy.interest().getAmount(), explicit.interest().getAmount());
        assertEquals(legacy.principal().getAmount(), explicit.principal().getAmount());
    }

    @Test
    public void legacyThirtyOneDayMonthDoesNotNormalize() {
        // Same dates as the DMI short golden but with NO config row: legacy counts 22 ACTUAL days (no -1).
        final BigDecimal principal = BigDecimal.valueOf(320000);
        givenLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2025, 7, 14), null);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 7, 5), LocalDate.of(2025, 8, 5), 1);
        // 320000*23/36000*22 = 4497.78 (not the DMI 4293.33)
        assertEquals(new BigDecimal("4497.78"), result.interest().getAmount());
    }

    // --- Dispatch edges ---

    @Test
    public void standardMethodReturnsStockUntouched() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.STANDARD);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        // stock values pass through even though the broken period is 45 days
        assertEquals(new BigDecimal("5720.00"), result.interest().getAmount());
    }

    @Test
    public void unknownMethodFailsLoudly() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), "ADJUSTED_DAY_SYMMETRIC"); // typo
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1));
    }

    @Test
    public void laterPeriodsOfFirstPeriodMethodsKeepStock() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 6, 5), LocalDate.of(2026, 7, 5), 2);
        assertEquals(new BigDecimal("5720.00"), result.interest().getAmount());
    }

    @Test
    public void memoizedMethodSkipsTheResolver() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        when(terms.isBrokenPeriodMethodResolved()).thenReturn(true);
        when(terms.getBrokenPeriodMethod()).thenReturn(BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        assertEquals(new BigDecimal("8580.00"), result.interest().getAmount());
        verifyNoInteractions(resolver);
    }

    @Test
    public void firstLookupIsMemoizedOnTheTerms() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        verify(terms).setBrokenPeriodMethod(BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        verify(terms).setBrokenPeriodMethodResolved(true);
    }

    // --- Northern Arc: DAYS_360_LEVEL_EMI (every period) ---
    // Golden schedules copied from Turno's NorthernArcEmiServiceImplTest, which match NAC's own schedules/demands.

    // NACL-generated schedule: 335000 @ 23%, 36 months, disbursed 2026-11-10, EMI 12968.
    private static final double[] NAC_335000_PRINCIPAL = { 7617.25, 6693.2, 6821.3, 6952.1, 7085.3, 7221.2, 7359.5, 7500.8, 7644.5, 7790.9,
            7940.3, 8092.4, 8247.5, 8405.6, 8566.7, 8731.1, 8898.2, 9068.9, 9242.6, 9419.9, 9600.5, 9784.4, 9971.9, 10163, 10358, 10556.3,
            10758.8, 10964.9, 11175.2, 11389.4, 11607.5, 11830.1, 12056.9, 12287.9, 12523.4, 10672.55 };
    private static final double[] NAC_335000_INTEREST = { 5350.75, 6274.8, 6146.7, 6015.9, 5882.7, 5746.8, 5608.5, 5467.2, 5323.5, 5177.1,
            5027.7, 4875.6, 4720.5, 4562.4, 4401.3, 4236.9, 4069.8, 3899.1, 3725.4, 3548.1, 3367.5, 3183.6, 2996.1, 2805, 2610, 2411.7,
            2209.2, 2003.1, 1792.8, 1578.6, 1360.5, 1137.9, 911.1, 680.1, 444.6, 204.6 };

    // NAC demand schedule for loan 363491: 337900 @ 23%, 36 months, disbursed 2026-11-03, EMI 13081.
    private static final double[] NAC_363491_PRINCIPAL = { 6172.84, 6722.8, 6851.8, 6983.2, 7117.0, 7253.2, 7392.4, 7534.0, 7678.3, 7825.6,
            7975.6, 8128.6, 8284.3, 8443.0, 8605.0, 8769.7, 8938.0, 9109.3, 9283.9, 9461.8, 9643.0, 9827.8, 10016.2, 10208.2, 10403.8,
            10603.3, 10806.4, 11013.7, 11224.9, 11440.0, 11659.3, 11882.5, 12110.5, 12342.4, 12579.1, 13608.56 };
    private static final double[] NAC_363491_INTEREST = { 6908.16, 6358.2, 6229.2, 6097.8, 5964.0, 5827.8, 5688.6, 5547.0, 5402.7, 5255.4,
            5105.4, 4952.4, 4796.7, 4638.0, 4476.0, 4311.3, 4143.0, 3971.7, 3797.1, 3619.2, 3438.0, 3253.2, 3064.8, 2872.8, 2677.2, 2477.7,
            2274.6, 2067.3, 1856.1, 1641.0, 1421.7, 1198.5, 970.5, 738.6, 501.9, 260.7 };

    private void givenNacLoan(final BigDecimal principal, final BigDecimal annualRate, final int tenure, final LocalDate disbursement) {
        when(resolver.resolve(PRODUCT_ID)).thenReturn(BrokenPeriodMethodResolver.DAYS_360_LEVEL_EMI);
        when(terms.getLoanProductId()).thenReturn(PRODUCT_ID);
        when(terms.getInterestCalculationPeriodMethod()).thenReturn(InterestCalculationPeriodMethod.SAME_AS_REPAYMENT_PERIOD);
        when(terms.getExpectedDisbursementDate()).thenReturn(disbursement);
        when(terms.getAnnualNominalInterestRate()).thenReturn(annualRate);
        when(terms.getNumberOfRepayments()).thenReturn(tenure);
        when(terms.getPrincipal()).thenReturn(Money.of(INR, principal, MC));
        when(terms.getPrincipalGrace()).thenReturn(0);
        when(terms.getInterestPaymentGrace()).thenReturn(0);
        when(terms.getInterestChargingGrace()).thenReturn(0);
        when(terms.getRepaymentPeriodFrequencyType()).thenReturn(PeriodFrequencyType.MONTHS);
        when(terms.getRepaymentEvery()).thenReturn(1);
        when(terms.isLastRepaymentPeriod(anyInt())).thenAnswer(inv -> (int) inv.getArgument(0) == tenure);
    }

    /** Drives the generator period by period the way the cumulative generator does (balance carried forward). */
    private void assertNacSchedule(final BigDecimal principal, final LocalDate firstDue, final double[] expectedPrincipal,
            final double[] expectedInterest) {
        Money balance = Money.of(INR, principal, MC);
        LocalDate due = firstDue;
        LocalDate periodStart = firstDue.minusMonths(1); // nominal month start under SAME_AS_REPAYMENT
        for (int i = 0; i < expectedPrincipal.length; i++) {
            final PrincipalInterest result = generator.calculatePrincipalInterestComponentsForPeriod(
                    mock(PaymentPeriodsInOneYearCalculator.class), BigDecimal.ZERO, Money.zero(INR, MC), Money.zero(INR, MC),
                    Money.zero(INR, MC), Money.zero(INR, MC), balance, terms, i + 1, MC, new TreeMap<>(), new HashMap<>(), periodStart, due,
                    new ArrayList<>());
            assertEquals(0, BigDecimal.valueOf(expectedInterest[i]).compareTo(result.interest().getAmount()),
                    "interest #" + (i + 1) + " was " + result.interest().getAmount());
            assertEquals(0, BigDecimal.valueOf(expectedPrincipal[i]).compareTo(result.principal().getAmount()),
                    "principal #" + (i + 1) + " was " + result.principal().getAmount());
            balance = balance.minus(result.principal());
            periodStart = due;
            due = due.plusMonths(1);
        }
        assertEquals(0, BigDecimal.ZERO.compareTo(balance.getAmount()), "closing balance");
    }

    @Test
    public void nacScheduleMatchesNaclScheduleFor335000() {
        givenNacLoan(BigDecimal.valueOf(335000), BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        assertNacSchedule(BigDecimal.valueOf(335000), LocalDate.of(2026, 12, 5), NAC_335000_PRINCIPAL, NAC_335000_INTEREST);
    }

    @Test
    public void nacScheduleMatchesNacDemandsForLoan363491() {
        givenNacLoan(BigDecimal.valueOf(337900), BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 3));
        assertNacSchedule(BigDecimal.valueOf(337900), LocalDate.of(2026, 12, 5), NAC_363491_PRINCIPAL, NAC_363491_INTEREST);
    }

    @Test
    public void nacFirstInstallmentEqualsCeiledEmi() {
        // 335000 @ 23%/36m: annuity 12967.xx -> 12968 (ceil); first installment = EMI despite the 25-day broken period.
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1);
        assertEquals(new BigDecimal("12968.00"), result.principal().getAmount().add(result.interest().getAmount()));
    }

    @Test
    public void nacLongBrokenPeriodUses30360Days() {
        // Disbursed 21-Nov-2026 (after cutoff 20), first EMI 05-Jan-2027: days360 = 44.
        // 100000*23/100 = 23000.00; /360 = 63.89; *44 = 2811.16.
        final BigDecimal principal = BigDecimal.valueOf(100000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 12, LocalDate.of(2026, 11, 21));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 12, 5), LocalDate.of(2027, 1, 5), 1);
        assertEquals(new BigDecimal("2811.16"), result.interest().getAmount());
    }

    @Test
    public void nacAnchorsOnInterestChargedFromDateWhenPresent() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 3));
        when(terms.getInterestChargedFromLocalDate()).thenReturn(LocalDate.of(2026, 11, 10));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1);
        assertEquals(new BigDecimal("5350.75"), result.interest().getAmount()); // 25 days from 10-Nov
    }

    @Test
    public void nacHonoursFixedEmiAmount() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getActualFixedEmiAmount()).thenReturn(BigDecimal.valueOf(13000));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1);
        assertEquals(new BigDecimal("13000.00"), result.principal().getAmount().add(result.interest().getAmount()));
    }

    @Test
    public void nacIgnoresGeneratorPrefilledEmi() {
        // The cumulative generator pre-fills getFixedEmiAmount() with its own 2dp PMT (12967.76 here); the ceiled
        // 12968 must still win because no EMI was supplied on the loan.
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getFixedEmiAmount()).thenReturn(new BigDecimal("12967.76"));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1);
        assertEquals(new BigDecimal("12968.00"), result.principal().getAmount().add(result.interest().getAmount()));
    }

    @Test
    public void nacRejectsInterestRecalculation() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.isInterestRecalculationEnabled()).thenReturn(true);
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacRejectsNonMonthlyFrequency() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getRepaymentPeriodFrequencyType()).thenReturn(PeriodFrequencyType.WEEKS);
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacRejectsNonSameAsRepaymentInterestPeriod() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getInterestCalculationPeriodMethod()).thenReturn(InterestCalculationPeriodMethod.DAILY);
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacRejectsGraceAndMultiDisbursement() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getPrincipalGrace()).thenReturn(1);
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
        when(terms.getPrincipalGrace()).thenReturn(0);
        when(terms.isMultiDisburseLoan()).thenReturn(true);
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacZeroRateLoanSplitsPrincipalEvenly() {
        final BigDecimal principal = BigDecimal.valueOf(12000);
        givenNacLoan(principal, BigDecimal.ZERO, 12, LocalDate.of(2026, 11, 10));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1);
        assertEquals(new BigDecimal("1000.00"), result.principal().getAmount());
        assertEquals(0, BigDecimal.ZERO.compareTo(result.interest().getAmount()));
    }

    @Test
    public void nacRejectsInterestStartAfterDueDate() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 12, 20));
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacRejectsInterestExceedingEmi() {
        final BigDecimal principal = BigDecimal.valueOf(335000);
        givenNacLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2026, 11, 10));
        when(terms.getActualFixedEmiAmount()).thenReturn(BigDecimal.valueOf(1000));
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 11, 5), LocalDate.of(2026, 12, 5), 1));
    }

    @Test
    public void nacDays360CapsBothDaysAt30() {
        assertEquals(25, BrokenPeriodDecliningBalanceLoanScheduleGenerator.days360(LocalDate.of(2026, 11, 10), LocalDate.of(2026, 12, 5)));
        assertEquals(30, BrokenPeriodDecliningBalanceLoanScheduleGenerator.days360(LocalDate.of(2027, 2, 5), LocalDate.of(2027, 3, 5)));
        assertEquals(44, BrokenPeriodDecliningBalanceLoanScheduleGenerator.days360(LocalDate.of(2026, 11, 21), LocalDate.of(2027, 1, 5)));
        assertEquals(5, BrokenPeriodDecliningBalanceLoanScheduleGenerator.days360(LocalDate.of(2026, 12, 31), LocalDate.of(2027, 1, 5)));
        assertEquals(32, BrokenPeriodDecliningBalanceLoanScheduleGenerator.days360(LocalDate.of(2026, 11, 3), LocalDate.of(2026, 12, 5)));
    }
}
